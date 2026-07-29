"""Bounded, deduplicated scheduling for site metadata analysis."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING
from weakref import ReferenceType, ref

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.db.database import Database
from webhub.db.models import Site, SiteMetadataPreference, utc_now

from . import backfill as metadata_backfill
from .enrichment import AnalysisIntent, SiteEnricher
from .service import (
    AUTO_PENDING_STALE_AFTER,
    AnalysisClaim,
    AnalysisProviderSignal,
    analyze_in_background,
    auto_backfill_site_ids,
)

if TYPE_CHECKING:
    from .fetcher import FetchOutcome

MAX_CONCURRENT_ANALYSES = 4
MAX_CONCURRENT_AUTO_ANALYSES = 2
# Historical work must never consume every network slot: keep one of the four
# available for an interactive save, card refresh, or explicit single-site
# retry.  Automatic and durable bulk runs share this budget.
MAX_CONCURRENT_BACKGROUND_ANALYSES = MAX_CONCURRENT_ANALYSES - 1
MAX_CONCURRENT_METADATA_BACKFILL_ANALYSES = 2
MAX_CONCURRENT_LLM_ANALYSES = 2
ANALYZE_WAIT_PENDING_TIMEOUT_SECONDS = 90
METADATA_BACKFILL_RECOVERY_BASE_DELAY_SECONDS = 1
METADATA_BACKFILL_RECOVERY_MAX_DELAY_SECONDS = 10
METADATA_BACKFILL_IDLE_RETRY_DELAY_SECONDS = 1
MAX_QUEUED_ANALYSES_PER_ACCOUNT = 256
MAX_QUEUED_ANALYSES_GLOBAL = 1_024
AUTO_DISCOVERY_BATCH_SIZE = 16
MAX_INTERACTIVE_QUEUE_OVERFLOW_PER_ACCOUNT = 1
MAX_INTERACTIVE_QUEUE_OVERFLOW_GLOBAL = MAX_CONCURRENT_ANALYSES

_LOGGER = logging.getLogger(__name__)


class AnalysisQueueFullError(RuntimeError):
    pass


class _MetadataItemExecutionBlocked(RuntimeError):
    """The durable item lost ownership or hit the persisted Provider fuse."""


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
    intents: dict[str, AnalysisIntent] = field(default_factory=dict)
    bulk_origins: dict[str, bool] = field(default_factory=dict)
    active_intents: dict[str, AnalysisIntent] = field(default_factory=dict)
    active_bulk_origins: dict[str, bool] = field(default_factory=dict)
    followup_intents: dict[str, AnalysisIntent] = field(default_factory=dict)
    followup_bulk_origins: dict[str, bool] = field(default_factory=dict)
    task: asyncio.Task[None] | None = None


@dataclass(slots=True)
class _AutoBackfill:
    database: Database
    user_id: str
    candidates: deque[str] = field(default_factory=deque)
    pending: set[str] = field(default_factory=set)
    completions: dict[str, asyncio.Future[FetchOutcome | None]] = field(default_factory=dict)
    followup_enrichment: dict[str, bool] = field(default_factory=dict)
    discovery_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    rescan_requested: bool = False
    task: asyncio.Task[None] | None = None


@dataclass(slots=True)
class _MetadataBackfill:
    database: Database
    user_id: str
    run_id: str
    next_run_id: str | None = None
    task: asyncio.Task[None] | None = None


_AccountKey = tuple[int, str]
_ACCOUNTS: dict[_AccountKey, _AccountQueue] = {}
_AUTO_BACKFILLS: dict[_AccountKey, _AutoBackfill] = {}
_METADATA_BACKFILLS: dict[_AccountKey, _MetadataBackfill] = {}
_STOPPED_DATABASES: dict[int, ReferenceType[object]] = {}
_STOPPED_STRONG_DATABASES: list[object] = []
_SITE_ENRICHERS: dict[int, SiteEnricher] = {}
_GLOBAL_SEMAPHORE: asyncio.Semaphore | None = None
_GLOBAL_SEMAPHORE_LOOP: asyncio.AbstractEventLoop | None = None
_AUTO_SEMAPHORE: asyncio.Semaphore | None = None
_AUTO_SEMAPHORE_LOOP: asyncio.AbstractEventLoop | None = None
_BACKGROUND_SEMAPHORE: asyncio.Semaphore | None = None
_BACKGROUND_SEMAPHORE_LOOP: asyncio.AbstractEventLoop | None = None
_LLM_SEMAPHORE: asyncio.Semaphore | None = None
_LLM_SEMAPHORE_LOOP: asyncio.AbstractEventLoop | None = None


def _account_key(database: Database, user_id: str) -> _AccountKey:
    return id(database), user_id


def _database_is_stopped(database: object) -> bool:
    stopped_database = _STOPPED_DATABASES.get(id(database))
    if stopped_database is not None and stopped_database() is database:
        return True
    return any(candidate is database for candidate in _STOPPED_STRONG_DATABASES)


def _mark_database_stopped(database: object) -> None:
    database_id = id(database)

    def remove(stopped_database: ReferenceType[object]) -> None:
        if _STOPPED_DATABASES.get(database_id) is stopped_database:
            _STOPPED_DATABASES.pop(database_id, None)

    try:
        stopped_database = ref(database, remove)
    except TypeError:
        if not any(candidate is database for candidate in _STOPPED_STRONG_DATABASES):
            _STOPPED_STRONG_DATABASES.append(database)
    else:
        _STOPPED_DATABASES[database_id] = stopped_database


def _mark_database_started(database: object) -> None:
    database_id = id(database)
    stopped_database = _STOPPED_DATABASES.get(database_id)
    if stopped_database is not None:
        registered_database = stopped_database()
        if registered_database is None or registered_database is database:
            _STOPPED_DATABASES.pop(database_id, None)
    _STOPPED_STRONG_DATABASES[:] = [
        candidate for candidate in _STOPPED_STRONG_DATABASES if candidate is not database
    ]


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


def _background_semaphore() -> asyncio.Semaphore:
    """Shared capacity for non-interactive metadata work."""

    global _BACKGROUND_SEMAPHORE, _BACKGROUND_SEMAPHORE_LOOP

    loop = asyncio.get_running_loop()
    if _BACKGROUND_SEMAPHORE is None or _BACKGROUND_SEMAPHORE_LOOP is not loop:
        _BACKGROUND_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_BACKGROUND_ANALYSES)
        _BACKGROUND_SEMAPHORE_LOOP = loop
    return _BACKGROUND_SEMAPHORE


def _llm_semaphore() -> asyncio.Semaphore:
    """Bound simultaneous Provider calls independently from page fetches."""

    global _LLM_SEMAPHORE, _LLM_SEMAPHORE_LOOP

    loop = asyncio.get_running_loop()
    if _LLM_SEMAPHORE is None or _LLM_SEMAPHORE_LOOP is not loop:
        _LLM_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_LLM_ANALYSES)
        _LLM_SEMAPHORE_LOOP = loop
    return _LLM_SEMAPHORE


async def _run_account(key: _AccountKey, state: _AccountQueue) -> None:
    async def consume() -> None:
        while state.site_ids:
            site_id = state.site_ids.popleft()
            completion = state.completions[site_id]
            intent = state.intents.pop(site_id, AnalysisIntent.METADATA_ONLY)
            bulk_origin = state.bulk_origins.pop(site_id, False)
            state.active_intents[site_id] = intent
            state.active_bulk_origins[site_id] = bulk_origin
            try:
                if intent is AnalysisIntent.SITE_ENRICHMENT:
                    async with _llm_semaphore(), _global_semaphore():
                        result = await analyze_in_background(
                            state.database,
                            state.user_id,
                            site_id,
                            bulk=state.active_bulk_origins.get(site_id, False),
                            use_llm=True,
                            enricher=_SITE_ENRICHERS.get(id(state.database)),
                        )
                else:
                    async with _global_semaphore():
                        result = await analyze_in_background(
                            state.database,
                            state.user_id,
                            site_id,
                        )
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
                followup = state.followup_intents.pop(site_id, None)
                followup_bulk = state.followup_bulk_origins.pop(site_id, False)
                state.pending.discard(site_id)
                state.interactive_overflow.discard(site_id)
                state.active_intents.pop(site_id, None)
                state.active_bulk_origins.pop(site_id, None)
                state.completions.pop(site_id, None)
                if (
                    followup is AnalysisIntent.SITE_ENRICHMENT
                    and not _database_is_stopped(state.database)
                ):
                    state.pending.add(site_id)
                    state.intents[site_id] = followup
                    state.bulk_origins[site_id] = followup_bulk
                    state.completions[site_id] = asyncio.get_running_loop().create_future()
                    state.site_ids.appendleft(site_id)

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
        if state.site_ids and not _database_is_stopped(state.database):
            _start(key, state)
        elif not state.pending or _database_is_stopped(state.database):
            _ACCOUNTS.pop(key, None)

    task.add_done_callback(finished)


async def _next_auto_site(
    state: _AutoBackfill,
) -> tuple[str, asyncio.Future[FetchOutcome | None], datetime] | None:
    """Lease one discovered row while retaining only a small per-account buffer."""

    async with state.discovery_lock:
        if _database_is_stopped(state.database):
            return None
        stale_before = utc_now() - AUTO_PENDING_STALE_AFTER
        while not _database_is_stopped(state.database):
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
    while not _database_is_stopped(state.database):
        async with _background_semaphore(), _auto_semaphore():
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
                followup_bulk = state.followup_enrichment.pop(site_id, None)
                state.pending.discard(site_id)
                state.completions.pop(site_id, None)
                if (
                    followup_bulk is not None
                    and not _database_is_stopped(state.database)
                ):
                    scheduled = schedule_analysis(
                        state.database,
                        user_id=state.user_id,
                        site_ids=(site_id,),
                        priority=True,
                        interactive=not followup_bulk,
                        intent=AnalysisIntent.SITE_ENRICHMENT,
                        bulk=followup_bulk,
                    )
                    if scheduled.rejected:
                        _LOGGER.warning(
                            "could not queue enrichment follow-up for %s",
                            site_id,
                        )


async def _run_auto_backfill(state: _AutoBackfill) -> None:
    while not _database_is_stopped(state.database):
        state.rescan_requested = False
        # A TaskGroup is intentional here. `gather` would let a sibling keep
        # fetching after one discovery task failed and after its state was
        # removed by the coordinator callback.
        async with asyncio.TaskGroup() as group:
            for _ in range(MAX_CONCURRENT_AUTO_ANALYSES):
                group.create_task(_consume_auto_backfill(state))
        if not state.rescan_requested:
            return


async def _release_metadata_item(
    state: _MetadataBackfill,
    lease: metadata_backfill.MetadataBackfillRunLease,
    item: metadata_backfill.MetadataBackfillItemClaim,
) -> None:
    """Best-effort cancellation cleanup; the durable item lease is the fallback."""

    try:
        async with state.database.sessions() as session:
            await metadata_backfill.release_item(session, lease, item)
    except Exception:  # noqa: BLE001 - the persisted lease will recover this item
        _LOGGER.exception("could not release metadata backfill item %s", item.id)


async def _heartbeat_metadata_item(
    state: _MetadataBackfill,
    lease: metadata_backfill.MetadataBackfillRunLease,
    item: metadata_backfill.MetadataBackfillItemClaim,
) -> None:
    """Keep a claimed row exclusive while it waits on network/model capacity."""

    loop = asyncio.get_running_loop()
    lease_seconds = metadata_backfill.ITEM_LEASE_DURATION.total_seconds()
    local_deadline = loop.time() + lease_seconds
    while True:
        remaining = local_deadline - loop.time()
        if remaining <= 0:
            return
        await asyncio.sleep(
            min(metadata_backfill.ITEM_LEASE_HEARTBEAT_SECONDS, remaining)
        )
        remaining = local_deadline - loop.time()
        if remaining <= 0:
            return
        renewal_started_at = loop.time()
        try:
            async with asyncio.timeout(remaining):
                async with state.database.sessions() as session:
                    renewed = await metadata_backfill.renew_item_lease(
                        session,
                        lease,
                        item,
                    )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return
        except Exception:  # noqa: BLE001 - the next heartbeat can recover a busy database
            _LOGGER.exception("could not heartbeat metadata backfill item %s", item.id)
            if loop.time() >= local_deadline:
                return
            continue
        if not renewed:
            return
        # The database lease is renewed from a timestamp captured inside the
        # call. Using the earlier local call time is conservative and never
        # lets this process believe it owns the row longer than storage does.
        local_deadline = renewal_started_at + lease_seconds


async def _metadata_item_needs_llm(
    state: _MetadataBackfill,
    item: metadata_backfill.MetadataBackfillItemClaim,
) -> bool:
    if not item.requires_llm:
        return False
    async with state.database.sessions() as session:
        preference = await session.get(
            SiteMetadataPreference,
            {"user_id": state.user_id, "site_id": item.site_id},
        )
    return preference is None or preference.llm_analyzed_at is None


async def _metadata_claim_miss_resolution(
    state: _MetadataBackfill,
    item: metadata_backfill.MetadataBackfillItemClaim,
) -> str:
    """Separate a temporary live claim from a real snapshot invalidation."""

    async with state.database.sessions() as session:
        site = await session.scalar(
            select(Site).where(Site.user_id == state.user_id, Site.id == item.site_id)
        )
        preference = await session.get(
            SiteMetadataPreference,
            {"user_id": state.user_id, "site_id": item.site_id},
        )
    if site is None or site.version != item.expected_version:
        return "skipped"
    if site.analysis_status == "pending":
        return "defer"
    llm_missing = item.requires_llm and (
        preference is None or preference.llm_analyzed_at is None
    )
    if llm_missing or site.analysis_status == "not_analyzed":
        return "defer"
    if site.analysis_status in {"complete", "limited", "failed"}:
        return site.analysis_status
    return "skipped"


async def _analyze_metadata_item_with_capacity(
    state: _MetadataBackfill,
    lease: metadata_backfill.MetadataBackfillRunLease,
    item: metadata_backfill.MetadataBackfillItemClaim,
    *,
    requires_llm: bool,
) -> FetchOutcome | None:
    """Run one immutable item claim under the capacity its snapshot requires."""

    async def persist_site_claim(
        session: AsyncSession,
        claim: AnalysisClaim,
    ) -> bool:
        # Keep the durable item token and pending Site identity in the same
        # transaction so restart recovery cannot steal either half.
        return await metadata_backfill.record_item_site_claim(
            session,
            lease,
            item,
            claimed_at=claim.claimed_at,
        )

    async def provider_call_is_allowed(session: AsyncSession) -> bool:
        return (
            await metadata_backfill.item_execution_intent(session, lease, item)
            is True
        )

    async def persist_provider_signal(
        session: AsyncSession,
        signal: AnalysisProviderSignal,
    ) -> bool:
        recorded = await metadata_backfill.record_provider_result(
            session,
            lease,
            item,
            failed=signal.failed,
            stop_batch=signal.stop_batch,
        )
        return recorded is not None

    async def execution_intent() -> bool:
        async with state.database.sessions() as session:
            current = await metadata_backfill.item_execution_intent(
                session,
                lease,
                item,
            )
        if current is None:
            raise _MetadataItemExecutionBlocked
        return current

    async def analyze_claimed_item(current_requires_llm: bool) -> FetchOutcome | None:
        return await analyze_in_background(
            state.database,
            state.user_id,
            item.site_id,
            bulk=True,
            expected_version=item.expected_version,
            expected_analysis_status=item.initial_analysis_status,
            expected_analysis_claimed_at=item.analysis_claimed_at,
            on_claimed=persist_site_claim,
            on_provider_signal=persist_provider_signal,
            before_provider_call=(
                provider_call_is_allowed if current_requires_llm else None
            ),
            propagate_errors=True,
            use_llm=current_requires_llm,
            enricher=(
                _SITE_ENRICHERS.get(id(state.database))
                if current_requires_llm
                else None
            ),
        )

    if requires_llm:
        async with _llm_semaphore(), _global_semaphore():
            return await analyze_claimed_item(await execution_intent())

    async with _global_semaphore():
        current_requires_llm = await execution_intent()
        if current_requires_llm:
            # The intent changed while waiting for network capacity. Requeue so
            # the next claim also acquires the bounded model semaphore.
            raise _MetadataItemExecutionBlocked
        return await analyze_claimed_item(False)


async def _consume_metadata_backfill(
    state: _MetadataBackfill,
    lease: metadata_backfill.MetadataBackfillRunLease,
) -> None:
    """Advance one run without holding more than one item or origin at a time."""

    while not _database_is_stopped(state.database):
        item: metadata_backfill.MetadataBackfillItemClaim | None = None
        outcome: FetchOutcome | None = None
        heartbeat: asyncio.Task[None] | None = None
        analysis_task: asyncio.Task[FetchOutcome | None] | None = None
        try:
            # Background capacity is shared with the quiet automatic sweep and
            # leaves one global network slot for interactive work. Once leased,
            # a heartbeat covers any wait for the account-wide model semaphore.
            async with _background_semaphore():
                async with state.database.sessions() as session:
                    item = await metadata_backfill.claim_next_item(session, lease)
                if item is None:
                    return
                heartbeat = asyncio.create_task(
                    _heartbeat_metadata_item(state, lease, item)
                )
                requires_llm = await _metadata_item_needs_llm(state, item)

                try:
                    analysis_task = asyncio.create_task(
                        _analyze_metadata_item_with_capacity(
                            state,
                            lease,
                            item,
                            requires_llm=requires_llm,
                        )
                    )
                    assert heartbeat is not None
                    done, _ = await asyncio.wait(
                        {analysis_task, heartbeat},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if heartbeat in done:
                        analysis_task.cancel()
                        await asyncio.gather(analysis_task, return_exceptions=True)
                        await _release_metadata_item(state, lease, item)
                        return
                    outcome = await analysis_task
                    if heartbeat.done():
                        await _release_metadata_item(state, lease, item)
                        return
                except _MetadataItemExecutionBlocked:
                    await _release_metadata_item(state, lease, item)
                    return
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - one remote site is terminal work
                    _LOGGER.exception("metadata backfill worker crashed for %s", item.site_id)
                    terminal_state = "failed"
                    analysis_crashed = True
                else:
                    terminal_state = outcome.status if outcome is not None else "skipped"
                    analysis_crashed = False

                if not analysis_crashed and outcome is None:
                    terminal_state = await _metadata_claim_miss_resolution(state, item)
                    if terminal_state == "defer":
                        async with state.database.sessions() as session:
                            deferred = await metadata_backfill.defer_item(
                                session,
                                lease,
                                item,
                            )
                        if not deferred:
                            return
                        continue

                async with state.database.sessions() as session:
                    finished = await metadata_backfill.finish_item(
                        session,
                        lease,
                        item,
                        state=terminal_state,
                    )
                if not finished:
                    return
        except asyncio.CancelledError:
            if analysis_task is not None and not analysis_task.done():
                analysis_task.cancel()
                await asyncio.gather(analysis_task, return_exceptions=True)
                analysis_task = None
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
                heartbeat = None
            if item is not None:
                await _release_metadata_item(state, lease, item)
            raise
        finally:
            if analysis_task is not None and not analysis_task.done():
                analysis_task.cancel()
                await asyncio.gather(analysis_task, return_exceptions=True)
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)


async def _run_metadata_backfill(state: _MetadataBackfill) -> None:
    """Keep an active persisted run advancing across transient SQLite errors."""

    recovery_attempts = 0
    while not _database_is_stopped(state.database):
        lease: metadata_backfill.MetadataBackfillRunLease | None = None
        retry_delay: float | None = None
        had_worker_error = False
        try:
            while not _database_is_stopped(state.database):
                async with state.database.sessions() as session:
                    lease = await metadata_backfill.acquire_run_lease(
                        session,
                        user_id=state.user_id,
                        run_id=state.run_id,
                    )
                    retry_delay = (
                        await metadata_backfill.lease_retry_delay(
                            session,
                            user_id=state.user_id,
                            run_id=state.run_id,
                        )
                        if lease is None
                        else None
                    )
                if lease is not None:
                    break
                if retry_delay is None:
                    return
                # A crash can leave a valid 90-second lease behind. Keep one
                # dormant coordinator around, so recovery never depends on a
                # browser status poll to wake it again.
                await asyncio.sleep(retry_delay)
            else:
                return

            assert lease is not None
            async with asyncio.TaskGroup() as group:
                for _ in range(MAX_CONCURRENT_METADATA_BACKFILL_ANALYSES):
                    group.create_task(_consume_metadata_backfill(state, lease))
            recovery_attempts = 0
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - durable work must survive a busy database
            had_worker_error = True
            recovery_attempts += 1
            _LOGGER.exception("metadata backfill coordinator paused for %s", state.run_id)
        finally:
            if lease is not None:
                # A normal terminal run no longer matches this token/state
                # update. On shutdown it immediately makes its small set of
                # leased rows runnable by the next API process instead of
                # making the UI wait for 90 seconds.
                try:
                    async with state.database.sessions() as session:
                        await metadata_backfill.release_run_lease(session, lease)
                except Exception:  # noqa: BLE001 - expiry remains a safe fallback
                    _LOGGER.exception("could not release metadata backfill run %s", state.run_id)

        if _database_is_stopped(state.database):
            return
        if had_worker_error:
            retry_delay = min(
                METADATA_BACKFILL_RECOVERY_BASE_DELAY_SECONDS * (2 ** (recovery_attempts - 1)),
                METADATA_BACKFILL_RECOVERY_MAX_DELAY_SECONDS,
            )
        else:
            # A recovered coordinator can briefly find only an orphaned item
            # lease. Avoid a tight acquire/return loop while still checking it
            # again without a page poll once that short lease becomes usable.
            retry_delay = METADATA_BACKFILL_IDLE_RETRY_DELAY_SECONDS
        await asyncio.sleep(retry_delay)


def ensure_auto_backfill(
    database: Database,
    user_id: str,
    *,
    rescan_if_running: bool = False,
) -> bool:
    """Idempotently start a database-driven, bounded sweep for one account."""

    if _database_is_stopped(database):
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
        state.followup_enrichment.clear()
        _AUTO_BACKFILLS.pop(key, None)

    task.add_done_callback(finished)
    return True


def ensure_metadata_backfill(
    database: Database,
    *,
    user_id: str,
    run_id: str,
) -> bool:
    """Idempotently wake one persisted batch; its database lease picks an owner."""

    if _database_is_stopped(database):
        return False
    key = _account_key(database, user_id)
    current = _METADATA_BACKFILLS.get(key)
    if current is not None and current.task is not None and not current.task.done():
        if current.run_id == run_id:
            return True
        current.next_run_id = run_id
        return True

    state = _MetadataBackfill(database=database, user_id=user_id, run_id=run_id)
    task = asyncio.create_task(_run_metadata_backfill(state))
    state.task = task
    _METADATA_BACKFILLS[key] = state

    def finished(completed: asyncio.Task[None]) -> None:
        if _METADATA_BACKFILLS.get(key) is not state:
            return
        if not completed.cancelled():
            error = completed.exception()
            if error is not None:
                _LOGGER.error("metadata backfill coordinator failed", exc_info=error)
        _METADATA_BACKFILLS.pop(key, None)
        if state.next_run_id is not None and not _database_is_stopped(state.database):
            ensure_metadata_backfill(
                state.database,
                user_id=state.user_id,
                run_id=state.next_run_id,
            )

    task.add_done_callback(finished)
    return True


async def resume_metadata_backfills(database: Database) -> None:
    """Wake all durable runs after an application process starts again."""

    if _database_is_stopped(database):
        return
    async with database.sessions() as session:
        active_runs = await metadata_backfill.list_active_runs(session)
    for user_id, run_id in active_runs:
        ensure_metadata_backfill(database, user_id=user_id, run_id=run_id)


def start(database: Database, *, site_enricher: SiteEnricher | None = None) -> None:
    """Allow a freshly started application to accept analysis work."""

    _mark_database_started(database)
    if site_enricher is None:
        _SITE_ENRICHERS.pop(id(database), None)
    else:
        _SITE_ENRICHERS[id(database)] = site_enricher


def schedule_analysis(
    database: Database,
    *,
    user_id: str,
    site_ids: list[str] | tuple[str, ...],
    priority: bool = False,
    interactive: bool = False,
    intent: AnalysisIntent = AnalysisIntent.METADATA_ONLY,
    bulk: bool = False,
) -> AnalysisSchedule:
    """Append bounded work, preserving whether LLM work came from a batch."""

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
        if state is not None and site_id in state.pending:
            if intent is AnalysisIntent.SITE_ENRICHMENT:
                if site_id in state.site_ids:
                    queued_intent = state.intents.get(
                        site_id,
                        AnalysisIntent.METADATA_ONLY,
                    )
                    state.intents[site_id] = AnalysisIntent.SITE_ENRICHMENT
                    if queued_intent is AnalysisIntent.SITE_ENRICHMENT:
                        state.bulk_origins[site_id] = (
                            state.bulk_origins.get(site_id, True) and bulk
                        )
                    else:
                        state.bulk_origins[site_id] = bulk
                elif state.active_intents.get(site_id) is AnalysisIntent.METADATA_ONLY:
                    state.followup_intents[site_id] = AnalysisIntent.SITE_ENRICHMENT
                    state.followup_bulk_origins[site_id] = (
                        state.followup_bulk_origins.get(site_id, True) and bulk
                    )
                elif state.active_intents.get(site_id) is AnalysisIntent.SITE_ENRICHMENT:
                    state.active_bulk_origins[site_id] = (
                        state.active_bulk_origins.get(site_id, True) and bulk
                    )
            already_queued += 1
            continue
        if auto_state is not None and site_id in auto_state.pending:
            if intent is AnalysisIntent.SITE_ENRICHMENT:
                auto_state.followup_enrichment[site_id] = (
                    auto_state.followup_enrichment.get(site_id, True) and bulk
                )
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
        if _database_is_stopped(database) or (
            account_full and not can_use_account_overflow
        ) or (global_full and not can_use_global_overflow):
            rejected += 1
            continue
        if state is None:
            state = _AccountQueue(database=database, user_id=user_id)
            _ACCOUNTS[key] = state
        state.pending.add(site_id)
        state.intents[site_id] = intent
        state.bulk_origins[site_id] = bulk
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
    """Run or join a true LLM enrichment without accepting metadata-only work."""

    async with database.sessions() as session:
        initial_preference = await session.get(
            SiteMetadataPreference,
            {"user_id": user_id, "site_id": site_id},
        )
        baseline_llm_analyzed_at = (
            initial_preference.llm_analyzed_at if initial_preference is not None else None
        )
        await session.rollback()

    async def site_is_failed() -> bool:
        async with database.sessions() as session:
            analysis_status = await session.scalar(
                select(Site.analysis_status).where(
                    Site.user_id == user_id,
                    Site.id == site_id,
                )
            )
            await session.rollback()
        return analysis_status == "failed"

    key = _account_key(database, user_id)
    while not _database_is_stopped(database):
        state = _ACCOUNTS.get(key)
        completion = state.completions.get(site_id) if state is not None else None
        if completion is not None and state is not None:
            active_intent = state.active_intents.get(site_id)
            queued_intent = state.intents.get(site_id)
            if active_intent is AnalysisIntent.SITE_ENRICHMENT:
                # A foreground single-site request is narrower than historical
                # batch work. If the Provider call is still waiting on
                # capacity, let the non-bulk origin win before it starts.
                state.active_bulk_origins[site_id] = False
                return await asyncio.shield(completion)
            if active_intent is None and queued_intent is not None:
                # The deque has not started this row yet, so upgrade it in
                # place without fetching the same page twice.
                state.intents[site_id] = AnalysisIntent.SITE_ENRICHMENT
                state.bulk_origins[site_id] = False
                return await asyncio.shield(completion)
            # A metadata-only fetch is already on the wire. Let it finish,
            # then enqueue the requested LLM pass instead of falsely treating
            # the lower-intent result as success.
            joined_outcome = await asyncio.shield(completion)
            if (
                (joined_outcome is not None and joined_outcome.status == "failed")
                or (joined_outcome is None and await site_is_failed())
            ):
                return None
            continue

        auto_state = _AUTO_BACKFILLS.get(key)
        auto_completion = (
            auto_state.completions.get(site_id) if auto_state is not None else None
        )
        if auto_completion is not None:
            joined_outcome = await asyncio.shield(auto_completion)
            if (
                (joined_outcome is not None and joined_outcome.status == "failed")
                or (joined_outcome is None and await site_is_failed())
            ):
                return None
            continue

        scheduled = schedule_analysis(
            database,
            user_id=user_id,
            site_ids=(site_id,),
            priority=True,
            interactive=True,
            intent=AnalysisIntent.SITE_ENRICHMENT,
            bulk=False,
        )
        if scheduled.queued != 1:
            if scheduled.already_queued:
                continue
            raise AnalysisQueueFullError("网站分析队列已满，请稍后重试")
        state = _ACCOUNTS[key]
        completion = state.completions[site_id]
        # A user waiting on this request should not sit behind a historical
        # metadata pass. No await occurred since enqueue, so this move is
        # atomic with respect to the account consumers.
        state.site_ids.remove(site_id)
        state.site_ids.appendleft(site_id)
        result = await asyncio.shield(completion)
        if result is not None:
            return result

        # A durable Q17 worker can own the database claim without appearing in
        # this process's in-memory queue. Wait for that exact work rather than
        # repeatedly stealing or duplicating a Provider request.
        wait_deadline = (
            asyncio.get_running_loop().time() + ANALYZE_WAIT_PENDING_TIMEOUT_SECONDS
        )
        while not _database_is_stopped(database):
            async with database.sessions() as session:
                site = await session.scalar(
                    select(Site).where(Site.user_id == user_id, Site.id == site_id)
                )
                preference = await session.get(
                    SiteMetadataPreference,
                    {"user_id": user_id, "site_id": site_id},
                )
            if site is None:
                return None
            if site.analysis_status == "pending":
                if asyncio.get_running_loop().time() >= wait_deadline:
                    raise AnalysisQueueFullError(
                        "该网站正由批量任务处理，请稍后查看分析结果"
                    )
                await asyncio.sleep(0.5)
                continue
            if site.analysis_status == "failed":
                return None
            current_llm_analyzed_at = (
                preference.llm_analyzed_at if preference is not None else None
            )
            if (
                current_llm_analyzed_at is not None
                and current_llm_analyzed_at != baseline_llm_analyzed_at
            ):
                return None
            # A metadata-only owner completed without advancing the LLM
            # marker. Loop once more to queue the requested enrichment pass.
            break

    raise AnalysisQueueFullError("网站分析服务正在停止，请稍后重试")


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
    _mark_database_stopped(database)
    states = [state for key, state in _ACCOUNTS.items() if key[0] == database_id]
    auto_states = [
        state for key, state in _AUTO_BACKFILLS.items() if key[0] == database_id
    ]
    metadata_states = [
        state for key, state in _METADATA_BACKFILLS.items() if key[0] == database_id
    ]
    tasks = [
        state.task
        for state in (*states, *auto_states, *metadata_states)
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
        state.intents.clear()
        state.bulk_origins.clear()
        state.active_intents.clear()
        state.active_bulk_origins.clear()
        state.followup_intents.clear()
        state.followup_bulk_origins.clear()
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
        state.followup_enrichment.clear()
        _AUTO_BACKFILLS.pop(key, None)
    for key in list(_METADATA_BACKFILLS):
        if key[0] == database_id:
            _METADATA_BACKFILLS.pop(key, None)
    _SITE_ENRICHERS.pop(database_id, None)


__all__ = [
    "MAX_CONCURRENT_ANALYSES",
    "MAX_CONCURRENT_AUTO_ANALYSES",
    "MAX_CONCURRENT_BACKGROUND_ANALYSES",
    "MAX_CONCURRENT_LLM_ANALYSES",
    "MAX_CONCURRENT_METADATA_BACKFILL_ANALYSES",
    "AUTO_DISCOVERY_BATCH_SIZE",
    "MAX_QUEUED_ANALYSES_GLOBAL",
    "MAX_QUEUED_ANALYSES_PER_ACCOUNT",
    "AnalysisQueueFullError",
    "AnalysisSchedule",
    "analyze_and_wait",
    "ensure_auto_backfill",
    "ensure_metadata_backfill",
    "pending_site_ids",
    "schedule_analysis",
    "resume_metadata_backfills",
    "shutdown",
    "start",
]
