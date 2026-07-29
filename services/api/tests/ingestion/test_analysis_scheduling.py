from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from webhub.bookmarks import routes as bookmark_routes
from webhub.bookmarks.schemas import BookmarkImportApplyRequest, BookmarkImportApplyResponse
from webhub.ingestion.enrichment import AnalysisIntent
from webhub.ingestion.worker import AnalysisSchedule
from webhub.library import routes as library_routes
from webhub.library.batch import BatchItem
from webhub.library.schemas import SiteBatchRequest, SiteUpdateRequest


def _request() -> Any:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(database=object())))


def _identity() -> Any:
    return SimpleNamespace(user=SimpleNamespace(id="user-1"))


def test_bookmark_apply_prioritizes_a_small_fresh_slice_then_starts_backfill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = BookmarkImportApplyResponse(
        job_id="job-1",
        state="completed",
        job_version=2,
        total_candidates=3,
        created=2,
        skipped_existing=1,
        skipped_needs_review=0,
        failed=0,
    )

    async def fake_apply(*_args: object, **_kwargs: object):
        return response

    scheduled: list[tuple[str, ...]] = []
    ensured: list[str] = []

    async def fake_recent(
        _session: object,
        user_id: str,
        *,
        limit: int,
    ) -> list[str]:
        assert user_id == "user-1"
        assert limit == 8
        return ["fresh-1", "fresh-2"]

    def fake_schedule(
        _database: object,
        *,
        user_id: str,
        site_ids: tuple[str, ...],
        priority: bool = False,
        interactive: bool = False,
        intent: AnalysisIntent,
        bulk: bool,
    ):
        assert user_id == "user-1"
        assert priority is True
        assert interactive is False
        assert intent is AnalysisIntent.SITE_ENRICHMENT
        assert bulk is True
        scheduled.append(site_ids)
        return AnalysisSchedule(queued=len(site_ids), already_queued=0, rejected=0)

    def fake_ensure(
        _database: object,
        *,
        user_id: str,
        rescan_if_running: bool = False,
    ) -> bool:
        assert rescan_if_running is True
        ensured.append(user_id)
        return True

    monkeypatch.setattr(bookmark_routes.queries, "apply_import", fake_apply)
    monkeypatch.setattr(
        bookmark_routes.ingestion_service,
        "recent_not_analyzed_site_ids",
        fake_recent,
    )
    monkeypatch.setattr(bookmark_routes.ingestion_worker, "schedule_analysis", fake_schedule)
    monkeypatch.setattr(bookmark_routes.ingestion_worker, "ensure_auto_backfill", fake_ensure)

    result = asyncio.run(
        bookmark_routes.apply_import(
            job_id="job-1",
            payload=BookmarkImportApplyRequest(expected_job_version=1),
            request=_request(),
            identity=_identity(),
            session=object(),  # type: ignore[arg-type]
            _=None,
        )
    )
    assert result == response
    assert scheduled == [("fresh-1", "fresh-2")]
    assert ensured == ["user-1"]


def test_confirmed_url_batch_queues_only_created_items(monkeypatch: pytest.MonkeyPatch) -> None:
    items = [
        BatchItem(url="https://one.example", status="created", site_id="site-1"),
        BatchItem(url="https://two.example", status="duplicate"),
        BatchItem(url="https://three.example", status="created", site_id="site-3"),
    ]

    async def fake_create(*_args: object, **_kwargs: object):
        return items

    captured: list[tuple[str, ...]] = []

    def fake_schedule(
        _database: object,
        *,
        user_id: str,
        site_ids: tuple[str, ...],
        priority: bool = False,
        interactive: bool = False,
        intent: AnalysisIntent,
        bulk: bool,
    ):
        assert user_id == "user-1"
        assert priority is False
        assert interactive is False
        assert intent is AnalysisIntent.SITE_ENRICHMENT
        assert bulk is True
        captured.append(site_ids)
        return AnalysisSchedule(queued=len(site_ids), already_queued=0, rejected=0)

    monkeypatch.setattr(library_routes.batch, "create_batch", fake_create)
    monkeypatch.setattr(library_routes.ingestion_worker, "schedule_analysis", fake_schedule)

    result = asyncio.run(
        library_routes.batch_sites(
            payload=SiteBatchRequest(urls=[item.url for item in items], confirm=True),
            request=_request(),
            identity=_identity(),
            session=object(),  # type: ignore[arg-type]
            _=None,
        )
    )
    assert result.created == 2
    assert captured == [("site-1", "site-3")]


def test_url_edit_queues_fresh_analysis_when_metadata_was_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_update(*_args: object, **_kwargs: object):
        return SimpleNamespace(id="site-1", analysis_status="not_analyzed")

    captured: list[tuple[str, ...]] = []

    def fake_schedule(
        _database: object,
        *,
        user_id: str,
        site_ids: tuple[str, ...],
        priority: bool = False,
        interactive: bool,
        intent: AnalysisIntent,
        bulk: bool,
    ):
        assert user_id == "user-1"
        assert priority is True
        assert interactive is True
        assert intent is AnalysisIntent.SITE_ENRICHMENT
        assert bulk is False
        captured.append(site_ids)
        return AnalysisSchedule(queued=1, already_queued=0, rejected=0)

    monkeypatch.setattr(library_routes.service, "update_site", fake_update)
    monkeypatch.setattr(library_routes.ingestion_worker, "schedule_analysis", fake_schedule)

    result = asyncio.run(
        library_routes.edit_site(
            site_id="site-1",
            payload=SiteUpdateRequest(expected_version=1, url="https://new.example"),
            request=_request(),
            identity=_identity(),
            session=object(),  # type: ignore[arg-type]
            _=None,
        )
    )
    assert result.analysis_status == "not_analyzed"
    assert captured == [("site-1",)]


def test_historical_backfill_is_explicit_bounded_and_skips_active_sites(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = {"site-active"}
    selected_exclusions: list[frozenset[str]] = []

    async def fake_select(
        _session: object,
        user_id: str,
        *,
        limit: int,
        excluded_site_ids: frozenset[str],
    ) -> tuple[list[str], int]:
        assert user_id == "user-1"
        assert limit == 256
        selected_exclusions.append(excluded_site_ids)
        return ["site-1", "site-2"], 7

    def fake_pending(_database: object, _user_id: str) -> frozenset[str]:
        return frozenset(active)

    def fake_schedule(
        _database: object,
        *,
        user_id: str,
        site_ids: tuple[str, ...],
        priority: bool = True,
        interactive: bool = False,
        intent: AnalysisIntent = AnalysisIntent.METADATA_ONLY,
        bulk: bool = False,
    ):
        assert user_id == "user-1"
        assert priority is False
        assert interactive is False
        assert intent is AnalysisIntent.METADATA_ONLY
        assert bulk is False
        active.update(site_ids)
        return AnalysisSchedule(queued=2, already_queued=0, rejected=0)

    monkeypatch.setattr(library_routes.ingestion_service, "not_analyzed_site_ids", fake_select)
    monkeypatch.setattr(library_routes.ingestion_worker, "pending_site_ids", fake_pending)
    monkeypatch.setattr(library_routes.ingestion_worker, "schedule_analysis", fake_schedule)

    result = asyncio.run(
        library_routes.analyze_missing_sites(
            request=_request(),
            identity=_identity(),
            session=object(),  # type: ignore[arg-type]
            _=None,
            limit=5_000,
        )
    )
    assert result.queued_count == 2
    assert result.active_count == 3
    assert result.remaining_count == 7
    assert selected_exclusions == [frozenset({"site-active"})]


def test_site_list_prioritizes_at_most_eight_visible_unanalyzed_sites(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = [
        SimpleNamespace(id=f"fresh-{index}", analysis_status="not_analyzed")
        for index in range(10)
    ] + [
        SimpleNamespace(id="failed", analysis_status="failed"),
        SimpleNamespace(id="limited", analysis_status="limited"),
    ]
    result_page = SimpleNamespace(items=items)

    async def fake_list(*_args: object, **_kwargs: object):
        return result_page

    scheduled: list[tuple[str, ...]] = []
    ensured: list[str] = []

    def fake_schedule(
        _database: object,
        *,
        user_id: str,
        site_ids: tuple[str, ...],
        priority: bool = False,
        interactive: bool = False,
        intent: AnalysisIntent = AnalysisIntent.METADATA_ONLY,
        bulk: bool = False,
    ):
        assert user_id == "user-1"
        assert priority is True
        assert interactive is False
        assert intent is AnalysisIntent.METADATA_ONLY
        assert bulk is False
        scheduled.append(site_ids)
        return AnalysisSchedule(queued=len(site_ids), already_queued=0, rejected=0)

    def fake_ensure(_database: object, *, user_id: str) -> bool:
        ensured.append(user_id)
        return len(ensured) == 1

    monkeypatch.setattr(library_routes.service, "list_sites", fake_list)
    monkeypatch.setattr(library_routes.ingestion_worker, "schedule_analysis", fake_schedule)
    monkeypatch.setattr(library_routes.ingestion_worker, "ensure_auto_backfill", fake_ensure)

    async def scenario() -> None:
        first = await library_routes.sites(
            request=_request(),
            identity=_identity(),
            session=object(),  # type: ignore[arg-type]
        )
        second = await library_routes.sites(
            request=_request(),
            identity=_identity(),
            session=object(),  # type: ignore[arg-type]
        )
        assert first is result_page
        assert second is result_page

    asyncio.run(scenario())
    expected = tuple(f"fresh-{index}" for index in range(8))
    assert scheduled == [expected, expected]
    assert ensured == ["user-1", "user-1"]
