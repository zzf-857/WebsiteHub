from __future__ import annotations

import asyncio
from typing import Any

import pytest

from webhub.ingestion import worker


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
        database = object()
        started = asyncio.Event()
        release = asyncio.Event()

        async def fake_analyze(*_args: object, **_kwargs: object) -> None:
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()

        monkeypatch.setattr(worker, "analyze_in_background", fake_analyze)
        worker.schedule_analysis(
            database,  # type: ignore[arg-type]
            user_id="join-user",
            site_ids=("site-1",),
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
        database = object()

        async def fake_analyze(_database: object, _user_id: str, site_id: str) -> None:
            order.append(site_id)
            await asyncio.sleep(0)

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
