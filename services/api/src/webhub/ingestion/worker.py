"""Bounded, deduplicated scheduling for site metadata analysis."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from webhub.db.database import Database
from webhub.db.models import utc_now

from .service import AUTO_PENDING_STALE_AFTER, analyze_in_background, auto_backfill_site_ids

if TYPE_CHECKING:
    from .fetcher import FetchOutcome

MAX_CONCURRENT_ANALYSES = 4
MAX_CONCURRENT_AUTO_ANALYSES = 2
MAX_QUEUED_ANALYSES_PER_ACCOUNT = 256
MAX_QUEUED_ANALYSES_GLOBAL = 1_024
AUTO_DISCOVERY_BATCH_SIZE = 16
MAX_INTERACTIVE_QUEUE_OVERFLOW_PER_ACCOUNT = 1
MAX_INTERACTIVE_QUEUE_OVERFLOW_GLOBAL = MAX_CONCURRENT_ANALYSES

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
    interactive_overflow: set[str] = field(default_factory=set)
    completions: dict[str, asyncio.Future[FetchOutcome | None]] = field(default_factory=dict)
    task: asyncio.Task[None] | None = None


@dataclass(slots=True)
class _AutoBackfill:
    database: Database
    user_id: str
    candidates: deque[str] = field(default_factory=deque)
    pending: set[str] = field(default_factory=set)
    completions: dict[str, asyncio.Future[FetchOutcome | None]] = field(default_factory=dict)
    discovery_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    rescan_requested: bool = False
    task: asyncio.Task[None] | None = None


_AccountKey = tuple[int, str]
_ACCOUNTS: dict[_AccountKey, _AccountQueue] = {}
_AUTO_BACKFILLS: dict[_AccountKey, _AutoBackfill] = {}
_STOPPED_DATABASES: set[int] = set()
_GLOBAL_SEMAPHORE: asyncio.Semaphore | None = None
_GLOBAL_SEMAPHORE_LOOP: asyncio.AbstractEventLoop | None = None
_AUTO_SEMAPHORE: asyncio.Semaphore | None = None
_AUTO_SEMAPHORE_LOOP: asyncio.AbstractEventLoop | None = None


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


def _auto_semaphore() -> asyncio.Semaphore:
    global _AUTO_SEMAPHORE, _AUTO_SEMAPHORE_LOOP

    loop = asyncio.get_running_loop()
    if _AUTO_SEMAPHORE is None or _AUTO_SEMAPHORE_LOOP is not loop:
        _AUTO_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_AUTO_ANALYSES)
        _AUTO_SEMAPHORE_LOOP = loop
    return _AUTO_SEMAPHORE


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
                state.interactive_overflow.discard(site_id)
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


async def _next_auto_site(
    state: _AutoBackfill,
) -> tuple[str, asyncio.Future[FetchOutcome | None], datetime] | None:
    """Lease one discovered row while retaining only a small per-account buffer."""

    async with state.discovery_lock:
        if id(state.database) in _STOPPED_DATABASES:
            return None
        stale_before = utc_now() - AUTO_PENDING_STALE_AFTER
        while id(state.database) not in _STOPPED_DATABASES:
            if not state.candidates:
                excluded = pending_site_ids(state.database, state.user_id)
                async with state.database.sessions() as session:
                    site_ids = await auto_backfill_site_ids(
                        session,
                        state.user_id,
                        limit=AUTO_DISCOVERY_BATCH_SIZE,
                        excluded_site_ids=excluded,
                        stale_before=stale_before,
                    )
                if not site_ids:
                    return None
                state.candidates.extend(site_ids)

            site_id = state.candidates.popleft()
            # Normal priority work can be scheduled while the database lookup
            # above is awaiting. Re-check after the await before leasing the
            # item, otherwise both queues may fetch the same remote page.
            if site_id in pending_site_ids(state.database, state.user_id):
                continue
            completion = asyncio.get_running_loop().create_future()
            state.pending.add(site_id)
            state.completions[site_id] = completion
            return site_id, completion, stale_before
        return None


async def _consume_auto_backfill(state: _AutoBackfill) -> None:
    while id(state.database) not in _STOPPED_DATABASES:
        async with _auto_semaphore():
            work = await _next_auto_site(state)
            if work is None:
                return
            site_id, completion, stale_before = work
            try:
                # Automatic work shares the global four-slot safety bound, but
                # its own semaphore guarantees it can occupy at most two slots.
                async with _global_semaphore():
                    result = await analyze_in_background(
                        state.database,
                        state.user_id,
                        site_id,
                        automatic=True,
                        stale_before=stale_before,
                    )
            except asyncio.CancelledError:
                if not completion.done():
                    completion.cancel()
                raise
            except Exception:  # noqa: BLE001 - one remote site must not stop the sweep
                _LOGGER.exception("automatic analysis worker crashed for %s", site_id)
                if not completion.done():
                    completion.set_result(None)
            else:
                if not completion.done():
                    completion.set_result(result)
            finally:
                state.pending.discard(site_id)
                state.completions.pop(site_id, None)


async def _run_auto_backfill(state: _AutoBackfill) -> None:
    while id(state.database) not in _STOPPED_DATABASES:
        state.rescan_requested = False
        # A TaskGroup is intentional here. `gather` would let a sibling keep
        # fetching after one discovery task failed and after its state was
        # removed by the coordinator callback.
        async with asyncio.TaskGroup() as group:
            for _ in range(MAX_CONCURRENT_AUTO_ANALYSES):
                group.create_task(_consume_auto_backfill(state))
        if not state.rescan_requested:
            return


def ensure_auto_backfill(
    database: Database,
    user_id: str,
    *,
    rescan_if_running: bool = False,
) -> bool:
    """Idempotently start a database-driven, bounded sweep for one account."""

    if id(database) in _STOPPED_DATABASES:
        return False
    key = _account_key(database, user_id)
    current = _AUTO_BACKFILLS.get(key)
    if current is not None and current.task is not None and not current.task.done():
        if rescan_if_running:
            current.rescan_requested = True
        return False

    state = _AutoBackfill(database=database, user_id=user_id)
    task = asyncio.create_task(_run_auto_backfill(state))
    state.task = task
    _AUTO_BACKFILLS[key] = state

    def finished(completed: asyncio.Task[None]) -> None:
        if _AUTO_BACKFILLS.get(key) is not state:
            return
        if not completed.cancelled():
            error = completed.exception()
            if error is not None:
                _LOGGER.error("automatic analysis coordinator failed", exc_info=error)
        for completion in state.completions.values():
            if not completion.done():
                completion.cancel()
        state.pending.clear()
        state.completions.clear()
        state.candidates.clear()
        _AUTO_BACKFILLS.pop(key, None)

    task.add_done_callback(finished)
    return True


def start(database: Database) -> None:
    """Allow a freshly started application to accept analysis work."""

    _STOPPED_DATABASES.discard(id(database))


def schedule_analysis(
    database: Database,
    *,
    user_id: str,
    site_ids: list[str] | tuple[str, ...],
    priority: bool = False,
    interactive: bool = False,
) -> AnalysisSchedule:
    """Append bounded work, with optional foreground ordering and one escape slot."""

    key = _account_key(database, user_id)
    state = _ACCOUNTS.get(key)
    auto_state = _AUTO_BACKFILLS.get(key)
    queued = already_queued = rejected = 0
    seen: set[str] = set()
    priority_site_ids: list[str] = []
    for raw_site_id in site_ids:
        site_id = raw_site_id.strip()
        if not site_id or site_id in seen:
            rejected += 1
            continue
        seen.add(site_id)
        if (state is not None and site_id in state.pending) or (
            auto_state is not None and site_id in auto_state.pending
        ):
            already_queued += 1
            continue
        account_pending = len(state.pending) if state is not None else 0
        global_pending = _global_pending_count()
        account_full = account_pending >= MAX_QUEUED_ANALYSES_PER_ACCOUNT
        global_full = global_pending >= MAX_QUEUED_ANALYSES_GLOBAL
        can_use_account_overflow = (
            interactive
            and state is not None
            and account_pending < (
                MAX_QUEUED_ANALYSES_PER_ACCOUNT + MAX_INTERACTIVE_QUEUE_OVERFLOW_PER_ACCOUNT
            )
            and len(state.interactive_overflow) < MAX_INTERACTIVE_QUEUE_OVERFLOW_PER_ACCOUNT
        )
        can_use_global_overflow = (
            interactive
            and global_pending < (
                MAX_QUEUED_ANALYSES_GLOBAL + MAX_INTERACTIVE_QUEUE_OVERFLOW_GLOBAL
            )
        )
        if id(database) in _STOPPED_DATABASES or (
            account_full and not can_use_account_overflow
        ) or (global_full and not can_use_global_overflow):
            rejected += 1
            continue
        if state is None:
            state = _AccountQueue(database=database, user_id=user_id)
            _ACCOUNTS[key] = state
        state.pending.add(site_id)
        if priority:
            priority_site_ids.append(site_id)
        else:
            state.site_ids.append(site_id)
        if account_full:
            state.interactive_overflow.add(site_id)
        state.completions[site_id] = asyncio.get_running_loop().create_future()
        queued += 1

    if state is not None and priority_site_ids:
        # `appendleft` reverses the source sequence, so reverse first to keep
        # a page's top-to-bottom visible order intact at the queue front.
        state.site_ids.extendleft(reversed(priority_site_ids))
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
        auto_state = _AUTO_BACKFILLS.get(key)
        completion = (
            auto_state.completions.get(site_id) if auto_state is not None else None
        )
    if completion is None:
        scheduled = schedule_analysis(
            database,
            user_id=user_id,
            site_ids=(site_id,),
            priority=True,
            interactive=True,
        )
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
    key = _account_key(database, user_id)
    state = _ACCOUNTS.get(key)
    auto_state = _AUTO_BACKFILLS.get(key)
    normal = state.pending if state is not None else set()
    automatic = auto_state.pending if auto_state is not None else set()
    return frozenset(normal | automatic)


async def shutdown(database: Database) -> None:
    """Stop accepting work, cancel active fetches, and drain their claim cleanup."""

    database_id = id(database)
    _STOPPED_DATABASES.add(database_id)
    states = [state for key, state in _ACCOUNTS.items() if key[0] == database_id]
    auto_states = [
        state for key, state in _AUTO_BACKFILLS.items() if key[0] == database_id
    ]
    tasks = [
        state.task
        for state in (*states, *auto_states)
        if state.task is not None and not state.task.done()
    ]
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
        state.interactive_overflow.clear()
        state.completions.clear()
        _ACCOUNTS.pop(key, None)
    for key, state in list(_AUTO_BACKFILLS.items()):
        if key[0] != database_id:
            continue
        for completion in state.completions.values():
            if not completion.done():
                completion.cancel()
        state.pending.clear()
        state.candidates.clear()
        state.completions.clear()
        _AUTO_BACKFILLS.pop(key, None)


__all__ = [
    "MAX_CONCURRENT_ANALYSES",
    "MAX_CONCURRENT_AUTO_ANALYSES",
    "AUTO_DISCOVERY_BATCH_SIZE",
    "MAX_QUEUED_ANALYSES_GLOBAL",
    "MAX_QUEUED_ANALYSES_PER_ACCOUNT",
    "AnalysisQueueFullError",
    "AnalysisSchedule",
    "analyze_and_wait",
    "ensure_auto_backfill",
    "pending_site_ids",
    "schedule_analysis",
    "shutdown",
    "start",
]
