from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any
from weakref import ref

import pytest

from webhub.ingestion import worker
from webhub.ingestion.fetcher import FetchOutcome


class _PreferenceSession:
    async def get(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def rollback(self) -> None:
        return None


class _PreferenceDatabase:
    @asynccontextmanager
    async def sessions(self):
        yield _PreferenceSession()


def test_stopped_database_registry_requires_object_identity() -> None:
    class EqualDatabase:
        def __eq__(self, other: object) -> bool:
            return isinstance(other, EqualDatabase)

        def __hash__(self) -> int:
            return 1

    stopped = EqualDatabase()
    running = EqualDatabase()
    worker._mark_database_stopped(stopped)  # noqa: SLF001 - registry regression
    try:
        assert worker._database_is_stopped(stopped) is True  # noqa: SLF001
        assert worker._database_is_stopped(running) is False  # noqa: SLF001

        # Deterministically model a stale id-keyed entry without relying on the
        # interpreter to reuse a recently collected object's address.
        worker._STOPPED_DATABASES[id(running)] = ref(stopped)  # noqa: SLF001
        assert worker._database_is_stopped(running) is False  # noqa: SLF001
    finally:
        worker._STOPPED_DATABASES.pop(id(running), None)  # noqa: SLF001
        worker._mark_database_started(stopped)  # noqa: SLF001


def test_analysis_queue_deduplicates_and_caps_execution_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = 0
    maximum_active = 0
    analyzed: list[str] = []

    async def fake_analyze(
        _database: Any,
        _user_id: str,
        site_id: str,
        **_kwargs: object,
    ) -> None:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0)
        analyzed.append(site_id)
        active -= 1

    monkeypatch.setattr(worker, "analyze_in_background", fake_analyze)

    async def scenario() -> None:
        user_id = "bounded-worker-test"
        database = object()
        first = worker.schedule_analysis(
            database,  # type: ignore[arg-type]
            user_id=user_id,
            site_ids=tuple(f"site-{index}" for index in range(20)),
        )
        second = worker.schedule_analysis(
            database,  # type: ignore[arg-type]
            user_id=user_id,
            site_ids=("site-1", "site-2", "site-20"),
        )
        assert first.queued == 20
        assert second.queued == 1
        assert second.already_queued == 2

        while worker.pending_site_ids(database, user_id):  # type: ignore[arg-type]
            await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert sorted(analyzed) == sorted(f"site-{index}" for index in range(21))
    assert maximum_active == worker.MAX_CONCURRENT_ANALYSES


def test_waiting_analysis_joins_existing_site_job(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def scenario() -> None:
        nonlocal calls
        database = _PreferenceDatabase()
        started = asyncio.Event()
        release = asyncio.Event()

        async def fake_analyze(*_args: object, **_kwargs: object) -> FetchOutcome:
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return FetchOutcome(status="complete", reason="test")

        monkeypatch.setattr(worker, "analyze_in_background", fake_analyze)
        worker.schedule_analysis(
            database,  # type: ignore[arg-type]
            user_id="join-user",
            site_ids=("site-1",),
            intent=worker.AnalysisIntent.SITE_ENRICHMENT,
        )
        waiter = asyncio.create_task(
            worker.analyze_and_wait(
                database,  # type: ignore[arg-type]
                user_id="join-user",
                site_id="site-1",
            )
        )
        await started.wait()
        release.set()
        await waiter

    asyncio.run(scenario())
    assert calls == 1


def test_waiting_analysis_moves_a_new_job_ahead_of_background_backlog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []

    async def scenario() -> None:
        database = _PreferenceDatabase()

        async def fake_analyze(
            _database: object,
            _user_id: str,
            site_id: str,
            **_kwargs: object,
        ) -> FetchOutcome:
            order.append(site_id)
            await asyncio.sleep(0)
            return FetchOutcome(status="complete", reason="test")

        monkeypatch.setattr(worker, "MAX_CONCURRENT_ANALYSES", 1)
        monkeypatch.setattr(worker, "analyze_in_background", fake_analyze)
        worker.schedule_analysis(
            database,  # type: ignore[arg-type]
            user_id="priority-user",
            site_ids=("background-1", "background-2"),
        )
        await worker.analyze_and_wait(
            database,  # type: ignore[arg-type]
            user_id="priority-user",
            site_id="interactive",
        )
        while worker.pending_site_ids(database, "priority-user"):  # type: ignore[arg-type]
            await asyncio.sleep(0)

    asyncio.run(scenario())
    assert order == ["interactive", "background-1", "background-2"]


def test_analysis_queue_rejects_work_beyond_its_account_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = asyncio.Event()

    async def stalled(*_args: object, **_kwargs: object) -> None:
        await release.wait()

    monkeypatch.setattr(worker, "MAX_QUEUED_ANALYSES_PER_ACCOUNT", 3)
    monkeypatch.setattr(worker, "analyze_in_background", stalled)

    async def scenario() -> None:
        user_id = "capacity-worker-test"
        database = object()
        result = worker.schedule_analysis(
            database,  # type: ignore[arg-type]
            user_id=user_id,
            site_ids=("one", "two", "three", "four", "five"),
        )
        assert result.queued == 3
        assert result.rejected == 2
        assert len(worker.pending_site_ids(database, user_id)) == 3  # type: ignore[arg-type]
        release.set()
        while worker.pending_site_ids(database, user_id):  # type: ignore[arg-type]
            await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(scenario())


def test_analysis_queue_caps_concurrency_across_accounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = 0
    maximum_active = 0

    async def scenario() -> None:
        nonlocal active, maximum_active

        release = asyncio.Event()
        limit_reached = asyncio.Event()

        async def fake_analyze(*_args: object, **_kwargs: object) -> None:
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            if active == worker.MAX_CONCURRENT_ANALYSES:
                limit_reached.set()
            try:
                await release.wait()
            finally:
                active -= 1

        monkeypatch.setattr(worker, "analyze_in_background", fake_analyze)
        database = object()
        user_ids = ("global-cap-alice", "global-cap-bob", "global-cap-carol")
        for user_id in user_ids:
            result = worker.schedule_analysis(
                database,  # type: ignore[arg-type]
                user_id=user_id,
                site_ids=tuple(f"{user_id}-site-{index}" for index in range(8)),
            )
            assert result.queued == 8

        await asyncio.wait_for(limit_reached.wait(), timeout=5)
        await asyncio.sleep(0)
        assert active == worker.MAX_CONCURRENT_ANALYSES
        release.set()
        while any(
            worker.pending_site_ids(database, user_id)  # type: ignore[arg-type]
            for user_id in user_ids
        ):
            await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert maximum_active == worker.MAX_CONCURRENT_ANALYSES


def test_analysis_queue_has_a_process_wide_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        database = object()
        release = asyncio.Event()

        async def stalled(*_args: object, **_kwargs: object) -> None:
            await release.wait()

        monkeypatch.setattr(worker, "MAX_QUEUED_ANALYSES_GLOBAL", 3)
        monkeypatch.setattr(worker, "analyze_in_background", stalled)
        first = worker.schedule_analysis(
            database,  # type: ignore[arg-type]
            user_id="global-one",
            site_ids=("one", "two"),
        )
        second = worker.schedule_analysis(
            database,  # type: ignore[arg-type]
            user_id="global-two",
            site_ids=("three", "four"),
        )
        assert first.queued == 2
        assert second.queued == 1
        assert second.rejected == 1
        release.set()
        while worker.pending_site_ids(  # type: ignore[arg-type]
            database, "global-one"
        ) or worker.pending_site_ids(database, "global-two"):  # type: ignore[arg-type]
            await asyncio.sleep(0)

    asyncio.run(scenario())


def test_auto_backfill_keeps_a_small_candidate_buffer_for_ten_thousand_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        class FakeDatabase:
            @asynccontextmanager
            async def sessions(self):
                yield object()

        database = FakeDatabase()
        user_id = "large-auto-backfill"
        site_ids = tuple(f"site-{index}" for index in range(10_000))
        started: set[str] = set()
        saturated = asyncio.Event()

        async def discover(
            _session: object,
            selected_user_id: str,
            *,
            limit: int,
            excluded_site_ids: frozenset[str],
            stale_before: object,
        ) -> list[str]:
            assert selected_user_id == user_id
            assert limit == worker.AUTO_DISCOVERY_BATCH_SIZE
            assert stale_before is not None
            return [
                site_id
                for site_id in site_ids
                if site_id not in excluded_site_ids
            ][:limit]

        async def stalled(
            _database: object,
            selected_user_id: str,
            site_id: str,
            **kwargs: object,
        ) -> None:
            assert selected_user_id == user_id
            assert kwargs["automatic"] is True
            started.add(site_id)
            if len(started) == worker.MAX_CONCURRENT_AUTO_ANALYSES:
                saturated.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(worker, "auto_backfill_site_ids", discover)
        monkeypatch.setattr(worker, "analyze_in_background", stalled)
        worker.start(database)  # type: ignore[arg-type]
        try:
            assert worker.ensure_auto_backfill(database, user_id) is True  # type: ignore[arg-type]
            assert worker.ensure_auto_backfill(database, user_id) is False  # type: ignore[arg-type]
            await asyncio.wait_for(saturated.wait(), timeout=5)

            key = (id(database), user_id)
            state = worker._AUTO_BACKFILLS[key]  # noqa: SLF001 - bounded-state assertion
            assert len(worker.pending_site_ids(database, user_id)) == 2  # type: ignore[arg-type]
            assert len(state.completions) == 2
            assert len(state.candidates) == worker.AUTO_DISCOVERY_BATCH_SIZE - 2
            assert len(started) == 2
        finally:
            await worker.shutdown(database)  # type: ignore[arg-type]

        assert worker.pending_site_ids(database, user_id) == frozenset()  # type: ignore[arg-type]
        assert (id(database), user_id) not in worker._AUTO_BACKFILLS  # noqa: SLF001

    asyncio.run(scenario())


def test_auto_backfill_cancel_drains_discovery_before_consumer_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery_cancelled = False
    discovery_drained = False

    async def scenario() -> None:
        nonlocal discovery_cancelled, discovery_drained
        database = object()
        state = worker._AutoBackfill(  # noqa: SLF001 - cancellation regression
            database=database,  # type: ignore[arg-type]
            user_id="drain-discovery-user",
        )
        discovery_started = asyncio.Event()
        release_discovery = asyncio.Event()

        async def controlled_discovery(
            _state: worker._AutoBackfill,  # noqa: SLF001
        ) -> None:
            nonlocal discovery_cancelled, discovery_drained
            discovery_started.set()
            try:
                await release_discovery.wait()
            except asyncio.CancelledError:
                discovery_cancelled = True
                raise
            finally:
                discovery_drained = True
            return None

        monkeypatch.setattr(worker, "_next_auto_site", controlled_discovery)
        consumer = asyncio.create_task(
            worker._consume_auto_backfill(state)  # noqa: SLF001
        )
        await asyncio.wait_for(discovery_started.wait(), timeout=5)
        consumer.cancel()
        release_discovery.set()
        with pytest.raises(asyncio.CancelledError):
            await consumer

    asyncio.run(scenario())
    assert discovery_drained is True
    assert discovery_cancelled is False
