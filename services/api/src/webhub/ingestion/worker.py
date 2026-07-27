"""Bounded, deduplicated scheduling for site metadata analysis."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from webhub.db.database import Database

from .service import analyze_in_background

if TYPE_CHECKING:
    from .fetcher import FetchOutcome

MAX_CONCURRENT_ANALYSES = 4
MAX_QUEUED_ANALYSES_PER_ACCOUNT = 5_000
MAX_QUEUED_ANALYSES_GLOBAL = 20_000

_LOGGER = logging.getLogger(__name__)


class AnalysisQueueFullError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AnalysisSchedule:
    queued: int
    already_queued: int
    rejected: int


@dataclass(slots=True)
class _AccountQueue:
    database: Database
    user_id: str
    site_ids: deque[str] = field(default_factory=deque)
    pending: set[str] = field(default_factory=set)
    completions: dict[str, asyncio.Future[FetchOutcome | None]] = field(default_factory=dict)
    task: asyncio.Task[None] | None = None


_AccountKey = tuple[int, str]
_ACCOUNTS: dict[_AccountKey, _AccountQueue] = {}
_STOPPED_DATABASES: set[int] = set()
_GLOBAL_SEMAPHORE: asyncio.Semaphore | None = None
_GLOBAL_SEMAPHORE_LOOP: asyncio.AbstractEventLoop | None = None


def _account_key(database: Database, user_id: str) -> _AccountKey:
    return id(database), user_id


def _global_pending_count() -> int:
    return sum(len(state.pending) for state in _ACCOUNTS.values())


def _global_semaphore() -> asyncio.Semaphore:
    global _GLOBAL_SEMAPHORE, _GLOBAL_SEMAPHORE_LOOP

    loop = asyncio.get_running_loop()
    if _GLOBAL_SEMAPHORE is None or _GLOBAL_SEMAPHORE_LOOP is not loop:
        _GLOBAL_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_ANALYSES)
        _GLOBAL_SEMAPHORE_LOOP = loop
    return _GLOBAL_SEMAPHORE


async def _run_account(key: _AccountKey, state: _AccountQueue) -> None:
    async def consume() -> None:
        while state.site_ids:
            site_id = state.site_ids.popleft()
            completion = state.completions[site_id]
            try:
                async with _global_semaphore():
                    result = await analyze_in_background(state.database, state.user_id, site_id)
            except asyncio.CancelledError:
                if not completion.done():
                    completion.cancel()
                raise
            except Exception:  # noqa: BLE001 - keep the worker alive after one bad job
                _LOGGER.exception("analysis worker crashed for %s", site_id)
                if not completion.done():
                    completion.set_result(None)
            else:
                if not completion.done():
                    completion.set_result(result)
            finally:
                state.pending.discard(site_id)
                state.completions.pop(site_id, None)

    worker_count = min(MAX_CONCURRENT_ANALYSES, len(state.site_ids))
    if worker_count:
        await asyncio.gather(*(consume() for _ in range(worker_count)))


def _start(key: _AccountKey, state: _AccountQueue) -> None:
    task = asyncio.create_task(_run_account(key, state))
    state.task = task

    def finished(completed: asyncio.Task[None]) -> None:
        current = _ACCOUNTS.get(key)
        if current is not state or state.task is not completed:
            return
        if not completed.cancelled():
            error = completed.exception()
            if error is not None:
                _LOGGER.error("analysis account worker failed", exc_info=error)
        if state.site_ids and id(state.database) not in _STOPPED_DATABASES:
            _start(key, state)
        elif not state.pending or id(state.database) in _STOPPED_DATABASES:
            _ACCOUNTS.pop(key, None)

    task.add_done_callback(finished)


def start(database: Database) -> None:
    """Allow a freshly started application to accept analysis work."""

    _STOPPED_DATABASES.discard(id(database))


def schedule_analysis(
    database: Database,
    *,
    user_id: str,
    site_ids: list[str] | tuple[str, ...],
) -> AnalysisSchedule:
    """Append work under both account and process-wide queue limits."""

    key = _account_key(database, user_id)
    state = _ACCOUNTS.get(key)
    queued = already_queued = rejected = 0
    seen: set[str] = set()
    for raw_site_id in site_ids:
        site_id = raw_site_id.strip()
        if not site_id or site_id in seen:
            rejected += 1
            continue
        seen.add(site_id)
        if state is not None and site_id in state.pending:
            already_queued += 1
            continue
        if (
            id(database) in _STOPPED_DATABASES
            or (state is not None and len(state.pending) >= MAX_QUEUED_ANALYSES_PER_ACCOUNT)
            or _global_pending_count() >= MAX_QUEUED_ANALYSES_GLOBAL
        ):
            rejected += 1
            continue
        if state is None:
            state = _AccountQueue(database=database, user_id=user_id)
            _ACCOUNTS[key] = state
        state.pending.add(site_id)
        state.site_ids.append(site_id)
        state.completions[site_id] = asyncio.get_running_loop().create_future()
        queued += 1

    if state is not None and state.site_ids and (state.task is None or state.task.done()):
        _start(key, state)
    return AnalysisSchedule(queued=queued, already_queued=already_queued, rejected=rejected)


async def analyze_and_wait(
    database: Database,
    *,
    user_id: str,
    site_id: str,
) -> FetchOutcome | None:
    """Join an active site analysis or queue it, then wait without cancelling shared work."""

    key = _account_key(database, user_id)
    state = _ACCOUNTS.get(key)
    completion = state.completions.get(site_id) if state is not None else None
    if completion is None:
        scheduled = schedule_analysis(database, user_id=user_id, site_ids=(site_id,))
        if scheduled.queued != 1:
            raise AnalysisQueueFullError("网站分析队列已满，请稍后重试")
        state = _ACCOUNTS[key]
        completion = state.completions[site_id]
        # A user waiting on this request should not sit behind a historical
        # backfill. The task has not yielded since enqueue, so moving it is
        # atomic with respect to the account consumers.
        state.site_ids.remove(site_id)
        state.site_ids.appendleft(site_id)
    return await asyncio.shield(completion)


def pending_site_ids(database: Database, user_id: str) -> frozenset[str]:
    state = _ACCOUNTS.get(_account_key(database, user_id))
    return frozenset(state.pending) if state is not None else frozenset()


async def shutdown(database: Database) -> None:
    """Stop accepting work, cancel active fetches, and drain their claim cleanup."""

    database_id = id(database)
    _STOPPED_DATABASES.add(database_id)
    states = [state for key, state in _ACCOUNTS.items() if key[0] == database_id]
    tasks = [state.task for state in states if state.task is not None and not state.task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    for key, state in list(_ACCOUNTS.items()):
        if key[0] != database_id:
            continue
        for completion in state.completions.values():
            if not completion.done():
                completion.cancel()
        state.site_ids.clear()
        state.pending.clear()
        state.completions.clear()
        _ACCOUNTS.pop(key, None)


__all__ = [
    "MAX_CONCURRENT_ANALYSES",
    "MAX_QUEUED_ANALYSES_GLOBAL",
    "MAX_QUEUED_ANALYSES_PER_ACCOUNT",
    "AnalysisQueueFullError",
    "AnalysisSchedule",
    "analyze_and_wait",
    "pending_site_ids",
    "schedule_analysis",
    "shutdown",
    "start",
]
