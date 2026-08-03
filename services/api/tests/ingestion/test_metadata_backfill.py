from __future__ import annotations

import asyncio
from datetime import UTC, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from webhub.db.database import Database
from webhub.db.migrations import upgrade_database
from webhub.db.models import (
    Category,
    Site,
    SiteMetadataBackfillItem,
    SiteMetadataBackfillRun,
    SiteMetadataPreference,
    User,
    utc_now,
)
from webhub.ingestion import backfill


def _database(tmp_path: Path) -> Database:
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'main.sqlite3').as_posix()}"
    upgrade_database(database_url)
    return Database(database_url)


async def _seed_selection_sites(database: Database) -> str:
    user_id = "backfill-user"
    category_id = "backfill-category"
    now = utc_now()
    async with database.sessions() as session:
        session.add(
            User(
                id=user_id,
                username="backfill-user",
                display_name="Backfill User",
                password_hash="test-hash",
            )
        )
        session.add(
            Category(
                id=category_id,
                user_id=user_id,
                name="未分类",
                normalized_name="未分类",
                is_default=True,
            )
        )
        site_ids = ("metadata-old", "llm-new", "llm-old", "metadata-new", "both-new")
        for index, site_id in enumerate(site_ids):
            requires_llm = site_id.startswith("llm-") or site_id.startswith("both-")
            requires_metadata = site_id.startswith("metadata-") or site_id.startswith("both-")
            created_at = now - timedelta(minutes=10 - index)
            session.add(
                Site(
                    id=site_id,
                    user_id=user_id,
                    category_id=category_id,
                    name=site_id,
                    normalized_name=site_id,
                    original_url=f"https://{site_id}.example/",
                    identity_url=f"https://{site_id}.example/",
                    position=index,
                    summary="" if requires_llm else "这是一个已经完成模型摘要的网站信息条目。",
                    description="" if requires_metadata else "已有详细介绍",
                    favicon_url=f"https://{site_id}.example/favicon.ico",
                    preview_url=f"https://{site_id}.example/preview.png",
                    analysis_status="complete",
                    analysis_updated_at=now,
                    created_at=created_at,
                    updated_at=now - timedelta(seconds=1),
                )
            )
            session.add(
                SiteMetadataPreference(
                    user_id=user_id,
                    site_id=site_id,
                    summary_is_llm=not requires_llm,
                    llm_analyzed_at=None if requires_llm else now,
                    preview_checked_at=now,
                )
            )

        session.add(
            Site(
                id="settled",
                user_id=user_id,
                category_id=category_id,
                name="settled",
                normalized_name="settled",
                original_url="https://settled.example/",
                identity_url="https://settled.example/",
                position=len(site_ids),
                summary="这是一个资料已经完整无需再次处理的网站。",
                description="已有详细介绍",
                favicon_url="https://settled.example/favicon.ico",
                preview_url="https://settled.example/preview.png",
                analysis_status="complete",
                analysis_updated_at=now,
                created_at=now,
                updated_at=now - timedelta(seconds=1),
            )
        )
        session.add(
            SiteMetadataPreference(
                user_id=user_id,
                site_id="settled",
                summary_is_llm=True,
                llm_analyzed_at=now,
                preview_checked_at=now,
            )
        )
        await session.commit()
    return user_id


def test_plan_reports_exact_mode_counts_and_limits(tmp_path: Path) -> None:
    database = _database(tmp_path)

    async def scenario() -> None:
        user_id = await _seed_selection_sites(database)
        async with database.sessions() as session:
            metadata = await backfill.plan_metadata_backfill(
                session,
                user_id=user_id,
                mode="metadata",
                limit=2,
            )
            full = await backfill.plan_metadata_backfill(
                session,
                user_id=user_id,
                mode="full",
                limit=3,
            )

        assert metadata == backfill.MetadataBackfillPlan(
            mode="metadata",
            requested_limit=2,
            eligible_count=3,
            selected_count=2,
            llm_count=0,
            max_limit=500,
        )
        assert full == backfill.MetadataBackfillPlan(
            mode="full",
            requested_limit=3,
            eligible_count=5,
            selected_count=3,
            llm_count=3,
            max_limit=100,
        )

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(database.dispose())

    assert backfill.metadata_backfill_limit("metadata") == 500
    assert backfill.metadata_backfill_limit("full") == 100
    invalid_database = _database(tmp_path / "invalid")
    with pytest.raises(ValueError, match="cannot exceed 100"):
        asyncio.run(_invalid_plan(invalid_database))
    asyncio.run(invalid_database.dispose())


async def _invalid_plan(database: Database) -> None:
    async with database.sessions() as session:
        await backfill.plan_metadata_backfill(
            session,
            user_id="nobody",
            mode="full",
            limit=101,
        )


@pytest.mark.parametrize(
    ("mode", "limit", "expected_ids", "expected_llm_count"),
    (
        ("metadata", 3, {"metadata-old", "metadata-new", "both-new"}, 0),
        ("full", 3, {"llm-old", "llm-new", "both-new"}, 3),
    ),
)
def test_start_freezes_a_bounded_mode_specific_snapshot(
    tmp_path: Path,
    mode: str,
    limit: int,
    expected_ids: set[str],
    expected_llm_count: int,
) -> None:
    database = _database(tmp_path)

    async def scenario() -> None:
        user_id = await _seed_selection_sites(database)
        async with database.sessions() as session:
            started = await backfill.start_metadata_backfill(
                session,
                user_id=user_id,
                mode=mode,
                limit=limit,
            )
            rows = list(
                (
                    await session.execute(
                        select(
                            SiteMetadataBackfillItem.site_id,
                            SiteMetadataBackfillItem.requires_llm,
                        ).where(SiteMetadataBackfillItem.run_id == started.progress.id)
                    )
                ).all()
            )

        assert started.reused is False
        assert started.progress.mode == mode
        assert started.progress.total_count == limit
        assert {str(site_id) for site_id, _ in rows} == expected_ids
        assert sum(bool(requires_llm) for _, requires_llm in rows) == expected_llm_count

        async with database.sessions() as session:
            lease = await backfill.acquire_run_lease(
                session,
                user_id=user_id,
                run_id=started.progress.id,
            )
        assert lease is not None
        assert lease.mode == mode

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(database.dispose())


async def _seed_running_fuse(
    database: Database,
) -> tuple[
    backfill.MetadataBackfillRunLease,
    backfill.MetadataBackfillItemClaim,
]:
    now = utc_now()
    lease_expiry = now + timedelta(minutes=5)
    user_id = "fuse-user"
    run_id = "fuse-run"
    token = "f" * 64
    async with database.sessions() as session:
        session.add(
            User(
                id=user_id,
                username="fuse-user",
                display_name="Fuse User",
                password_hash="test-hash",
            )
        )
        session.add(
            SiteMetadataBackfillRun(
                id=run_id,
                user_id=user_id,
                mode="full",
                state="running",
                total_count=3,
                queued_count=2,
                running_count=1,
                lease_token_hash=token,
                lease_expires_at=lease_expiry,
                heartbeat_at=now,
            )
        )
        await session.flush()
        session.add_all(
            [
                SiteMetadataBackfillItem(
                    id="running-item",
                    user_id=user_id,
                    run_id=run_id,
                    site_id="running-site",
                    expected_version=1,
                    initial_analysis_status="complete",
                    requires_llm=True,
                    origin_key="https://running.example",
                    state="running",
                    attempt_count=1,
                    lease_token_hash=token,
                    lease_expires_at=lease_expiry,
                ),
                SiteMetadataBackfillItem(
                    id="untouched-item",
                    user_id=user_id,
                    run_id=run_id,
                    site_id="untouched-site",
                    expected_version=1,
                    initial_analysis_status="complete",
                    requires_llm=True,
                    origin_key="https://untouched.example",
                    state="queued",
                    attempt_count=0,
                ),
                SiteMetadataBackfillItem(
                    id="retried-item",
                    user_id=user_id,
                    run_id=run_id,
                    site_id="retried-site",
                    expected_version=1,
                    initial_analysis_status="complete",
                    requires_llm=True,
                    origin_key="https://retried.example",
                    state="queued",
                    attempt_count=1,
                ),
            ]
        )
        await session.commit()
    return (
        backfill.MetadataBackfillRunLease(user_id=user_id, run_id=run_id, token_hash=token),
        backfill.MetadataBackfillItemClaim(
            id="running-item",
            site_id="running-site",
            expected_version=1,
            initial_analysis_status="complete",
            attempt_count=1,
            analysis_claimed_at=None,
            origin_key="https://running.example",
            token_hash=token,
            requires_llm=True,
        ),
    )


def test_rate_limit_cooldown_then_fuse_skips_untouched_work(tmp_path: Path) -> None:
    database = _database(tmp_path)

    async def scenario() -> None:
        lease, item = await _seed_running_fuse(database)
        async with database.sessions() as session:
            for failure_number in (1, 2):
                stopped = await backfill.record_provider_result(
                    session,
                    lease,
                    item,
                    failed=True,
                    failure_reason="provider_rate_limited",
                    retry_after_seconds=10_000,
                )
                assert stopped is False
                retry_at = await session.scalar(
                    select(SiteMetadataBackfillRun.provider_retry_at).where(
                        SiteMetadataBackfillRun.id == lease.run_id
                    )
                )
                assert retry_at is not None
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                remaining = (retry_at - utc_now()).total_seconds()
                assert 295 <= remaining <= 300
                failures = await session.scalar(
                    select(SiteMetadataBackfillRun.consecutive_provider_failures).where(
                        SiteMetadataBackfillRun.id == lease.run_id
                    )
                )
                assert failures == failure_number

            stopped = await backfill.record_provider_result(
                session,
                lease,
                item,
                failed=True,
                failure_reason="provider_rate_limited",
                retry_after_seconds=10_000,
            )
            assert stopped is True

            run = await session.get(SiteMetadataBackfillRun, lease.run_id)
            assert run is not None
            assert run.stop_requested is True
            assert run.stop_reason == "provider_rate_limited"
            assert run.provider_retry_at is not None
            assert run.queued_count == 0
            assert run.running_count == 1
            assert run.failed_count == 1
            assert run.skipped_count == 1
            states = dict(
                (
                    await session.execute(
                        select(
                            SiteMetadataBackfillItem.id,
                            SiteMetadataBackfillItem.state,
                        ).where(SiteMetadataBackfillItem.run_id == lease.run_id)
                    )
                ).all()
            )
            assert states == {
                "running-item": "running",
                "untouched-item": "skipped",
                "retried-item": "failed",
            }

            assert await backfill.finish_item(
                session,
                lease,
                item,
                state="limited",
            )
            progress = await backfill.progress_for_run(
                session,
                user_id=lease.user_id,
                run_id=lease.run_id,
            )
            assert progress is not None
            assert progress.status == "failed"
            assert progress.stop_reason == "provider_rate_limited"
            assert progress.limited_count == 1
            assert progress.failed_count == 1
            assert progress.skipped_count == 1

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(database.dispose())
