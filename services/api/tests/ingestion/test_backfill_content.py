from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.db.database import Database
from webhub.db.migrations import upgrade_database
from webhub.db.models import (
    Category,
    Site,
    SiteContent,
    SiteMetadataBackfillItem,
    SiteMetadataBackfillRun,
    User,
)
from webhub.ingestion import backfill, service, worker
from webhub.ingestion.enrichment import SiteEnrichmentRequest, SiteEnrichmentResult
from webhub.ingestion.fetcher import FetchOutcome, SiteMetadata

BODY = "这是一篇用于验证旧收藏正文快照的公开文章，介绍收藏检索和资料整理的方法。" * 8
OLD = datetime(2020, 1, 1, tzinfo=UTC)


class _Enricher:
    async def enrich(self, request: SiteEnrichmentRequest) -> SiteEnrichmentResult:
        return SiteEnrichmentResult(
            category_id=request.current_category_id,
            existing_tag_ids=(),
            new_tag_names=("知识管理", "资料整理"),
            summary="帮助读者通过整理公开资料建立方便检索与重温的个人收藏库",
            description="该公开文章介绍如何记录资料来源并整理分类标签，帮助读者建立能够持续使用的个人收藏库。"
            * 3,
        )


async def _seed(database: Database) -> None:
    async with database.sessions() as session:
        session.add(User(id="alice", username="alice", display_name="Alice", password_hash="test"))
        await session.flush()
        session.add(
            Category(
                id="category",
                user_id="alice",
                name="未分类",
                normalized_name="未分类",
                is_default=True,
            )
        )
        await session.flush()
        session.add(
            Site(
                id="legacy",
                user_id="alice",
                category_id="category",
                name="旧收藏",
                normalized_name="旧收藏",
                original_url="https://example.test/",
                identity_url="https://example.test/",
                position=0,
                created_at=OLD,
                updated_at=OLD,
                version=4,
            )
        )
        await session.commit()


@pytest.mark.parametrize("mode", ["metadata", "full"])
@pytest.mark.parametrize("snapshot_fails", [False, True])
def test_explicit_backfill_stores_legacy_content_without_affecting_item_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    snapshot_fails: bool,
) -> None:
    _run_backfill(tmp_path, monkeypatch, mode=mode, snapshot_fails=snapshot_fails, body=BODY)


def test_backfill_without_page_text_leaves_snapshot_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run_backfill(tmp_path, monkeypatch, mode="metadata", snapshot_fails=False, body="")


def _run_backfill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: str,
    snapshot_fails: bool,
    body: str,
) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'main.sqlite3'}"
    upgrade_database(url)
    database = Database(url)

    async def fetch(_url: str, *, timeout_seconds: int) -> FetchOutcome:
        return FetchOutcome(
            status="complete",
            reason="ok",
            metadata=SiteMetadata(
                title="公开文章",
                description="这篇公开文章介绍如何整理收藏并通过标签检索已保存资料。",
                page_text=body,
            ),
        )

    snapshot_versions: list[tuple[tuple[object, ...], tuple[object, ...]]] = []
    store_snapshot = service.store_content_snapshot

    async def observed_snapshot(session: AsyncSession, **kwargs: object) -> bool:
        query = select(Site.version, Site.updated_at).where(
            Site.user_id == "alice",
            Site.id == "legacy",
        )
        before = tuple((await session.execute(query)).one())
        if snapshot_fails:
            raise RuntimeError("simulated local storage failure")
        stored = await store_snapshot(session, **kwargs)
        after = tuple((await session.execute(query)).one())
        snapshot_versions.append((before, after))
        return stored

    monkeypatch.setattr(service, "fetch_site_metadata", fetch)
    monkeypatch.setattr(service, "store_content_snapshot", observed_snapshot)

    async def scenario() -> None:
        await _seed(database)
        worker.start(database, site_enricher=_Enricher())
        try:
            async with database.sessions() as session:
                assert await session.get(SiteContent, "legacy") is None
                started = await backfill.start_metadata_backfill(
                    session,
                    user_id="alice",
                    mode=mode,
                    limit=1,
                )
            assert started.progress.total_count == 1
            assert worker.ensure_metadata_backfill(
                database,
                user_id="alice",
                run_id=started.progress.id,
            )
            state = worker._METADATA_BACKFILLS[worker._account_key(database, "alice")]
            assert state.task is not None
            await asyncio.wait_for(asyncio.shield(state.task), timeout=10)
            async with database.sessions() as session:
                item = await session.scalar(
                    select(SiteMetadataBackfillItem).where(
                        SiteMetadataBackfillItem.user_id == "alice",
                        SiteMetadataBackfillItem.run_id == started.progress.id,
                    )
                )
                run = await session.get(SiteMetadataBackfillRun, started.progress.id)
                assert item.state == "complete"
                assert run.complete_count == 1
                assert run.failed_count == 0
                content = await session.get(SiteContent, "legacy")
                if snapshot_fails or not body:
                    assert content is None
                else:
                    assert content.user_id == "alice"
                    assert content.content == body
                    assert content.source == "page"
                site = await session.get(Site, "legacy")
                if mode == "metadata":
                    assert site.version == 4
                    assert site.updated_at.replace(tzinfo=UTC) == OLD
                if body and not snapshot_fails:
                    assert len(snapshot_versions) == 1
                    assert snapshot_versions[0][0] == snapshot_versions[0][1]
        finally:
            await worker.shutdown(database)
            await database.dispose()

    asyncio.run(scenario())
