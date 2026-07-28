"""Durable, low-priority metadata backfill bookkeeping.

This module owns the database state machine, not the outbound work.  The
worker claims one item at a time through these functions and uses the existing
safe fetcher, so a progress bar has a fixed denominator without creating a
second, unbounded network queue.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import and_, case, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.db.models import (
    Site,
    SiteMetadataBackfillItem,
    SiteMetadataBackfillRun,
    new_id,
    utc_now,
)

from .service import (
    AUTO_PENDING_STALE_AFTER,
    llm_enrichment_missing_condition,
    metadata_backfill_eligibility,
)

SNAPSHOT_CHUNK_SIZE = 256
RUN_LEASE_DURATION = timedelta(seconds=90)
ITEM_LEASE_DURATION = timedelta(seconds=90)
ITEM_LEASE_HEARTBEAT_SECONDS = 20
ITEM_DEFER_DURATION = timedelta(seconds=5)
MAX_CONSECUTIVE_PROVIDER_FAILURES = 3
PROVIDER_RETRY_BASE_SECONDS = 5
PROVIDER_RETRY_MAX_SECONDS = 60
MAX_LEASE_RETRY_DELAY_SECONDS = 10
_ACTIVE_RUN_STATES = ("queued", "running")
_ITEM_TERMINAL_STATES = ("complete", "limited", "failed", "skipped")


@dataclass(frozen=True, slots=True)
class MetadataBackfillProgress:
    id: str
    status: str
    stopped_early: bool
    total_count: int
    queued_count: int
    running_count: int
    completed_count: int
    complete_count: int
    limited_count: int
    failed_count: int
    skipped_count: int

    @property
    def is_active(self) -> bool:
        return self.status in {"queued", "running"}


@dataclass(frozen=True, slots=True)
class MetadataBackfillStart:
    progress: MetadataBackfillProgress
    reused: bool


@dataclass(frozen=True, slots=True)
class MetadataBackfillRunLease:
    user_id: str
    run_id: str
    token_hash: str


@dataclass(frozen=True, slots=True)
class MetadataBackfillItemClaim:
    id: str
    site_id: str
    expected_version: int
    initial_analysis_status: str
    attempt_count: int
    analysis_claimed_at: datetime | None
    origin_key: str
    token_hash: str
    requires_llm: bool = True


def _origin_key(identity_url: str) -> str:
    """Return the normalized scheme/host/port portion of a stored URL."""

    parts = urlsplit(identity_url)
    if not parts.scheme or not parts.netloc:
        # Library writes already normalize URLs.  This defensive fallback keeps
        # an older malformed row isolated rather than blocking the entire run.
        return identity_url[:320] or "unknown"
    return f"{parts.scheme.casefold()}://{parts.netloc.casefold()}"[:320]


def _run_token_hash() -> str:
    return hashlib.sha256(secrets.token_bytes(32)).hexdigest()


def _progress_from_run(run: SiteMetadataBackfillRun) -> MetadataBackfillProgress:
    """Project a run's denormalized counters without scanning its items.

    Item state remains the recovery source of truth, but every state transition
    updates these counters in the same SQLite transaction.  The status endpoint
    is therefore O(1), including for a batch with tens of thousands of rows.
    """

    queued = run.queued_count
    running = run.running_count
    complete = run.complete_count
    limited = run.limited_count
    failed = run.failed_count
    skipped = run.skipped_count
    completed = complete + limited + failed + skipped
    status = (
        "failed"
        if run.stop_requested and run.state not in _ACTIVE_RUN_STATES
        else run.state
    )
    return MetadataBackfillProgress(
        id=run.id,
        status=status,
        stopped_early=run.stop_requested,
        total_count=run.total_count,
        queued_count=queued,
        running_count=running,
        completed_count=completed,
        complete_count=complete,
        limited_count=limited,
        failed_count=failed,
        skipped_count=skipped,
    )


async def progress_for_run(
    session: AsyncSession,
    *,
    user_id: str,
    run_id: str,
) -> MetadataBackfillProgress | None:
    run = await session.scalar(
        select(SiteMetadataBackfillRun).where(
            SiteMetadataBackfillRun.user_id == user_id,
            SiteMetadataBackfillRun.id == run_id,
        )
    )
    if run is None:
        return None
    return _progress_from_run(run)


async def active_metadata_backfill(
    session: AsyncSession,
    *,
    user_id: str,
) -> MetadataBackfillProgress | None:
    """Return the account's one active run so a refreshed page can reattach."""

    active = await _active_run(session, user_id=user_id)
    if active is None:
        return None
    return _progress_from_run(active)


async def _active_run(
    session: AsyncSession,
    *,
    user_id: str,
) -> SiteMetadataBackfillRun | None:
    return await session.scalar(
        select(SiteMetadataBackfillRun)
        .where(
            SiteMetadataBackfillRun.user_id == user_id,
            SiteMetadataBackfillRun.state.in_(_ACTIVE_RUN_STATES),
        )
        .order_by(SiteMetadataBackfillRun.created_at, SiteMetadataBackfillRun.id)
        .limit(1)
    )


async def start_metadata_backfill(
    session: AsyncSession,
    *,
    user_id: str,
) -> MetadataBackfillStart:
    """Freeze one account's eligible targets without retaining them in memory."""

    active = await _active_run(session, user_id=user_id)
    if active is not None:
        return MetadataBackfillStart(
            progress=_progress_from_run(active),
            reused=True,
        )

    # SQLite read transactions cannot always be upgraded to a writer after a
    # concurrent tab commits. End the negative lookup before making the small
    # active-run insert so the partial unique index, rather than
    # SQLITE_BUSY_SNAPSHOT, decides which request joins.
    await session.rollback()

    snapshot_started_at = utc_now()
    run = SiteMetadataBackfillRun(user_id=user_id, state="queued", total_count=0)
    session.add(run)
    total = 0
    try:
        await session.flush()
        last_created_at: datetime | None = None
        last_id: str | None = None
        eligibility = metadata_backfill_eligibility(
            stale_before=snapshot_started_at - AUTO_PENDING_STALE_AFTER,
        )
        requires_llm = llm_enrichment_missing_condition().label("requires_llm")
        while True:
            conditions: list[object] = [
                Site.user_id == user_id,
                Site.created_at <= snapshot_started_at,
                eligibility,
            ]
            if last_created_at is not None and last_id is not None:
                conditions.append(
                    or_(
                        Site.created_at > last_created_at,
                        and_(Site.created_at == last_created_at, Site.id > last_id),
                    )
                )
            rows = list(
                (
                    await session.execute(
                        select(
                            Site.id,
                            Site.version,
                            Site.analysis_status,
                            requires_llm,
                            Site.identity_url,
                            Site.created_at,
                        )
                        .where(*conditions)
                        .order_by(Site.created_at, Site.id)
                        .limit(SNAPSHOT_CHUNK_SIZE)
                    )
                ).all()
            )
            if not rows:
                break

            now = utc_now()
            values = [
                {
                    "id": new_id(),
                    "user_id": user_id,
                    "run_id": run.id,
                    "site_id": site_id,
                    "expected_version": version,
                    "initial_analysis_status": analysis_status,
                    "requires_llm": bool(item_requires_llm),
                    "origin_key": _origin_key(identity_url),
                    "state": "queued",
                    "attempt_count": 0,
                    "created_at": now,
                    "updated_at": now,
                }
                for (
                    site_id,
                    version,
                    analysis_status,
                    item_requires_llm,
                    identity_url,
                    _,
                ) in rows
            ]
            await session.execute(SiteMetadataBackfillItem.__table__.insert(), values)
            total += len(values)
            _, _, _, _, _, last_created_at = rows[-1]
            last_id = str(rows[-1][0])

        # The snapshot is invisible until this transaction commits. Publish the
        # fixed denominator and its initial queued state together so counters
        # always sum to the number of immutable item rows.
        run.total_count = total
        run.queued_count = total
        if total == 0:
            run.state = "completed"
            run.completed_at = utc_now()
        await session.commit()
    except IntegrityError:
        # A second click or tab may have inserted the partial-unique active run
        # after the read above.  Roll back all of this attempt and join it.
        await session.rollback()
        active = await _active_run(session, user_id=user_id)
        if active is None:
            raise
        return MetadataBackfillStart(
            progress=_progress_from_run(active),
            reused=True,
        )
    except OperationalError:
        # A short SQLite writer collision is expected when two tabs press the
        # command together. If the other request has now published its run,
        # join it instead of returning a spurious 500; otherwise preserve the
        # real database error for the API's normal error handler.
        await session.rollback()
        active = await _active_run(session, user_id=user_id)
        if active is None:
            raise
        return MetadataBackfillStart(
            progress=_progress_from_run(active),
            reused=True,
        )
    except BaseException:
        await session.rollback()
        raise

    return MetadataBackfillStart(
        progress=_progress_from_run(run),
        reused=False,
    )


async def list_active_runs(
    session: AsyncSession,
) -> list[tuple[str, str]]:
    rows = await session.execute(
        select(SiteMetadataBackfillRun.user_id, SiteMetadataBackfillRun.id).where(
            SiteMetadataBackfillRun.state.in_(_ACTIVE_RUN_STATES)
        )
    )
    return [(str(user_id), str(run_id)) for user_id, run_id in rows]


async def acquire_run_lease(
    session: AsyncSession,
    *,
    user_id: str,
    run_id: str,
) -> MetadataBackfillRunLease | None:
    """Become the sole local process allowed to advance this persisted run."""

    now = utc_now()
    token_hash = _run_token_hash()
    acquired = await session.execute(
        update(SiteMetadataBackfillRun)
        .where(
            SiteMetadataBackfillRun.user_id == user_id,
            SiteMetadataBackfillRun.id == run_id,
            SiteMetadataBackfillRun.state.in_(_ACTIVE_RUN_STATES),
            or_(
                SiteMetadataBackfillRun.lease_expires_at.is_(None),
                SiteMetadataBackfillRun.lease_expires_at < now,
            ),
        )
        .values(
            state="running",
            lease_token_hash=token_hash,
            lease_expires_at=now + RUN_LEASE_DURATION,
            heartbeat_at=now,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if acquired.rowcount != 1:  # type: ignore[attr-defined]
        await session.rollback()
        return None
    await session.commit()
    return MetadataBackfillRunLease(user_id=user_id, run_id=run_id, token_hash=token_hash)


async def lease_retry_delay(
    session: AsyncSession,
    *,
    user_id: str,
    run_id: str,
) -> float | None:
    """Return a quiet retry delay while another process still owns an active run."""

    run = await session.scalar(
        select(SiteMetadataBackfillRun).where(
            SiteMetadataBackfillRun.user_id == user_id,
            SiteMetadataBackfillRun.id == run_id,
        )
    )
    if run is None or run.state not in _ACTIVE_RUN_STATES:
        return None
    if run.lease_expires_at is None:
        return 0.5
    expires_at = run.lease_expires_at
    if expires_at.tzinfo is None:
        # SQLite stores the absolute timestamp but does not round-trip tzinfo.
        expires_at = expires_at.replace(tzinfo=UTC)
    remaining = (expires_at - utc_now()).total_seconds()
    return min(max(remaining, 0.5), MAX_LEASE_RETRY_DELAY_SECONDS)


async def _renew_run_lease(
    session: AsyncSession,
    lease: MetadataBackfillRunLease,
    *,
    now: datetime,
) -> bool:
    renewed = await session.execute(
        update(SiteMetadataBackfillRun)
        .where(
            SiteMetadataBackfillRun.user_id == lease.user_id,
            SiteMetadataBackfillRun.id == lease.run_id,
            SiteMetadataBackfillRun.state == "running",
            SiteMetadataBackfillRun.lease_token_hash == lease.token_hash,
            SiteMetadataBackfillRun.lease_expires_at >= now,
        )
        .values(
            lease_expires_at=now + RUN_LEASE_DURATION,
            heartbeat_at=now,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    return renewed.rowcount == 1  # type: ignore[attr-defined]


async def _adjust_run_counts(
    session: AsyncSession,
    lease: MetadataBackfillRunLease,
    *,
    queued_delta: int = 0,
    running_delta: int = 0,
    complete_delta: int = 0,
    limited_delta: int = 0,
    failed_delta: int = 0,
    skipped_delta: int = 0,
) -> bool:
    """Move run counters with the corresponding item state transition.

    Every caller has already conditionally updated its item row in the current
    transaction.  Keeping the run update token-scoped makes a lost lease roll
    both writes back together instead of publishing misleading progress.
    """

    deltas = {
        "queued_count": queued_delta,
        "running_count": running_delta,
        "complete_count": complete_delta,
        "limited_count": limited_delta,
        "failed_count": failed_delta,
        "skipped_count": skipped_delta,
    }
    values = {
        column: getattr(SiteMetadataBackfillRun, column) + delta
        for column, delta in deltas.items()
        if delta
    }
    if not values:
        return True

    conditions: list[object] = [
        SiteMetadataBackfillRun.user_id == lease.user_id,
        SiteMetadataBackfillRun.id == lease.run_id,
        SiteMetadataBackfillRun.state == "running",
        SiteMetadataBackfillRun.lease_token_hash == lease.token_hash,
    ]
    for column, delta in deltas.items():
        if delta < 0:
            conditions.append(getattr(SiteMetadataBackfillRun, column) >= -delta)

    adjusted = await session.execute(
        update(SiteMetadataBackfillRun)
        .where(*conditions)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    return adjusted.rowcount == 1  # type: ignore[attr-defined]


async def _lock_run_stop_state(
    session: AsyncSession,
    lease: MetadataBackfillRunLease,
) -> bool | None:
    """Serialize a running-item release with Provider fuse transitions."""

    locked = await session.execute(
        update(SiteMetadataBackfillRun)
        .where(
            SiteMetadataBackfillRun.user_id == lease.user_id,
            SiteMetadataBackfillRun.id == lease.run_id,
            SiteMetadataBackfillRun.state == "running",
            SiteMetadataBackfillRun.lease_token_hash == lease.token_hash,
        )
        # SQLite takes its writer reservation here; multi-writer databases
        # take the run row lock. Explicitly preserve the timestamp.
        .values(updated_at=SiteMetadataBackfillRun.updated_at)
        .execution_options(synchronize_session=False)
    )
    if locked.rowcount != 1:  # type: ignore[attr-defined]
        return None
    stop_requested = await session.scalar(
        select(SiteMetadataBackfillRun.stop_requested).where(
            SiteMetadataBackfillRun.user_id == lease.user_id,
            SiteMetadataBackfillRun.id == lease.run_id,
            SiteMetadataBackfillRun.state == "running",
            SiteMetadataBackfillRun.lease_token_hash == lease.token_hash,
        )
    )
    return bool(stop_requested)


async def _requeue_expired_items(
    session: AsyncSession,
    lease: MetadataBackfillRunLease,
    *,
    now: datetime,
    stop_requested: bool,
) -> bool:
    conditions = (
        SiteMetadataBackfillItem.user_id == lease.user_id,
        SiteMetadataBackfillItem.run_id == lease.run_id,
        SiteMetadataBackfillItem.state == "running",
        SiteMetadataBackfillItem.lease_expires_at < now,
    )
    expired_claims: list[tuple[str, datetime]] = []
    if stop_requested:
        expired_claims = [
            (str(site_id), claimed_at)
            for site_id, claimed_at in (
                await session.execute(
                    select(
                        SiteMetadataBackfillItem.site_id,
                        SiteMetadataBackfillItem.analysis_claimed_at,
                    ).where(*conditions)
                )
            ).all()
            if claimed_at is not None
        ]
    transitioned = await session.execute(
        update(SiteMetadataBackfillItem)
        .where(*conditions)
        .values(
            state="failed" if stop_requested else "queued",
            analysis_claimed_at=(
                None
                if stop_requested
                else SiteMetadataBackfillItem.analysis_claimed_at
            ),
            lease_token_hash=None,
            lease_expires_at=None,
            available_at=None,
            completed_at=now if stop_requested else None,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    count = int(transitioned.rowcount or 0)  # type: ignore[attr-defined]
    if count == 0:
        return True
    if stop_requested:
        for site_id, claimed_at in expired_claims:
            await session.execute(
                update(Site)
                .where(
                    Site.user_id == lease.user_id,
                    Site.id == site_id,
                    Site.analysis_status == "pending",
                    Site.analysis_updated_at == claimed_at,
                )
                .values(
                    analysis_status="not_analyzed",
                    analysis_updated_at=now,
                    updated_at=Site.updated_at,
                )
            )
    return await _adjust_run_counts(
        session,
        lease,
        running_delta=-count,
        **({"failed_delta": count} if stop_requested else {"queued_delta": count}),
    )


async def claim_next_item(
    session: AsyncSession,
    lease: MetadataBackfillRunLease,
) -> MetadataBackfillItemClaim | None:
    """Lease one queued row, never in parallel with another row of its origin."""

    for _ in range(4):
        now = utc_now()
        if not await _renew_run_lease(session, lease, now=now):
            await session.rollback()
            return None
        run_state = (
            await session.execute(
                select(
                    SiteMetadataBackfillRun.stop_requested,
                    or_(
                        SiteMetadataBackfillRun.provider_retry_at.is_(None),
                        SiteMetadataBackfillRun.provider_retry_at <= now,
                    ).label("provider_ready"),
                ).where(
                    SiteMetadataBackfillRun.user_id == lease.user_id,
                    SiteMetadataBackfillRun.id == lease.run_id,
                    SiteMetadataBackfillRun.state == "running",
                    SiteMetadataBackfillRun.lease_token_hash == lease.token_hash,
                )
            )
        ).one_or_none()
        if run_state is None:
            await session.rollback()
            return None
        stop_requested, provider_ready = run_state
        if not await _requeue_expired_items(
            session,
            lease,
            now=now,
            stop_requested=stop_requested,
        ):
            await session.rollback()
            return None
        if stop_requested:
            failed_count = await _fail_queued_items_locked(session, lease, now=now)
            if failed_count is None:
                await session.rollback()
                return None
            await _finish_run_if_terminal(session, lease, now=now)
            await session.commit()
            return None
        if not provider_ready:
            await session.commit()
            return None
        active_origins = set(
            (
                await session.scalars(
                    select(SiteMetadataBackfillItem.origin_key).where(
                        SiteMetadataBackfillItem.user_id == lease.user_id,
                        SiteMetadataBackfillItem.run_id == lease.run_id,
                        SiteMetadataBackfillItem.state == "running",
                    )
                )
            ).all()
        )
        conditions: list[object] = [
            SiteMetadataBackfillItem.user_id == lease.user_id,
            SiteMetadataBackfillItem.run_id == lease.run_id,
            SiteMetadataBackfillItem.state == "queued",
            or_(
                SiteMetadataBackfillItem.available_at.is_(None),
                SiteMetadataBackfillItem.available_at <= now,
            ),
        ]
        if active_origins:
            conditions.append(SiteMetadataBackfillItem.origin_key.not_in(active_origins))
        item = await session.scalar(
            select(SiteMetadataBackfillItem)
            .where(*conditions)
            .order_by(SiteMetadataBackfillItem.created_at, SiteMetadataBackfillItem.id)
            .limit(1)
        )
        if item is None:
            await session.commit()
            return None
        try:
            next_attempt_count = item.attempt_count + 1
            claimed = await session.execute(
                update(SiteMetadataBackfillItem)
                .where(
                    SiteMetadataBackfillItem.id == item.id,
                    SiteMetadataBackfillItem.user_id == lease.user_id,
                    SiteMetadataBackfillItem.run_id == lease.run_id,
                    SiteMetadataBackfillItem.state == "queued",
                    SiteMetadataBackfillItem.attempt_count == item.attempt_count,
                )
                .values(
                    state="running",
                    attempt_count=next_attempt_count,
                    lease_token_hash=lease.token_hash,
                    lease_expires_at=now + ITEM_LEASE_DURATION,
                    available_at=None,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
        except IntegrityError:
            # Another consumer claimed this origin between the advisory read
            # above and this state transition. The partial unique index is the
            # authority; retry with a fresh view rather than losing the run.
            await session.rollback()
            continue
        if claimed.rowcount == 1:  # type: ignore[attr-defined]
            if not await _adjust_run_counts(
                session,
                lease,
                queued_delta=-1,
                running_delta=1,
            ):
                await session.rollback()
                return None
            await session.commit()
            return MetadataBackfillItemClaim(
                id=item.id,
                site_id=item.site_id,
                expected_version=item.expected_version,
                initial_analysis_status=item.initial_analysis_status,
                attempt_count=next_attempt_count,
                requires_llm=item.requires_llm,
                analysis_claimed_at=item.analysis_claimed_at,
                origin_key=item.origin_key,
                token_hash=lease.token_hash,
            )
        await session.rollback()
    return None


async def record_item_site_claim(
    session: AsyncSession,
    lease: MetadataBackfillRunLease,
    item: MetadataBackfillItemClaim,
    *,
    claimed_at: datetime,
) -> bool:
    """Persist the exact Site pending token before outbound work begins.

    This intentionally does not commit. ``_claim_analysis`` calls it in the
    same transaction as changing the Site to pending, so a crash cannot leave
    an item without the only token allowed to reclaim that pending claim.
    """

    now = utc_now()
    active_run = select(SiteMetadataBackfillRun.id).where(
        SiteMetadataBackfillRun.user_id == lease.user_id,
        SiteMetadataBackfillRun.id == lease.run_id,
        SiteMetadataBackfillRun.state == "running",
        SiteMetadataBackfillRun.stop_requested.is_(False),
        SiteMetadataBackfillRun.lease_token_hash == lease.token_hash,
        SiteMetadataBackfillRun.lease_expires_at >= now,
    ).exists()
    recorded = await session.execute(
        update(SiteMetadataBackfillItem)
        .where(
            SiteMetadataBackfillItem.id == item.id,
            SiteMetadataBackfillItem.user_id == lease.user_id,
            SiteMetadataBackfillItem.run_id == lease.run_id,
            SiteMetadataBackfillItem.state == "running",
            SiteMetadataBackfillItem.lease_token_hash == item.token_hash,
            SiteMetadataBackfillItem.attempt_count == item.attempt_count,
            SiteMetadataBackfillItem.lease_expires_at >= now,
            active_run,
        )
        .values(analysis_claimed_at=claimed_at, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    return recorded.rowcount == 1  # type: ignore[attr-defined]


async def item_execution_intent(
    session: AsyncSession,
    lease: MetadataBackfillRunLease,
    item: MetadataBackfillItemClaim,
) -> bool | None:
    """Revalidate ownership/fuse and return the current LLM requirement.

    ``None`` means the item must not start outbound work. The check sits after
    all capacity waits, closing both a lost-lease window and the race where a
    separate analysis completed LLM enrichment while this item was waiting.
    """

    now = utc_now()
    row = (
        await session.execute(
            select(
                SiteMetadataBackfillItem.requires_llm,
                llm_enrichment_missing_condition(),
            )
            .select_from(SiteMetadataBackfillRun)
            .join(
                SiteMetadataBackfillItem,
                and_(
                    SiteMetadataBackfillItem.user_id
                    == SiteMetadataBackfillRun.user_id,
                    SiteMetadataBackfillItem.run_id == SiteMetadataBackfillRun.id,
                ),
            )
            .join(
                Site,
                and_(
                    Site.user_id == SiteMetadataBackfillItem.user_id,
                    Site.id == SiteMetadataBackfillItem.site_id,
                ),
            )
            .where(
                SiteMetadataBackfillRun.user_id == lease.user_id,
                SiteMetadataBackfillRun.id == lease.run_id,
                SiteMetadataBackfillRun.state == "running",
                SiteMetadataBackfillRun.stop_requested.is_(False),
                or_(
                    SiteMetadataBackfillRun.provider_retry_at.is_(None),
                    SiteMetadataBackfillRun.provider_retry_at <= now,
                ),
                SiteMetadataBackfillRun.lease_token_hash == lease.token_hash,
                SiteMetadataBackfillRun.lease_expires_at >= now,
                SiteMetadataBackfillItem.id == item.id,
                SiteMetadataBackfillItem.state == "running",
                SiteMetadataBackfillItem.lease_token_hash == item.token_hash,
                SiteMetadataBackfillItem.attempt_count == item.attempt_count,
                SiteMetadataBackfillItem.lease_expires_at >= now,
            )
        )
    ).one_or_none()
    if row is None:
        return None
    requires_llm, enrichment_missing = row
    return bool(requires_llm and enrichment_missing)


async def renew_item_lease(
    session: AsyncSession,
    lease: MetadataBackfillRunLease,
    item: MetadataBackfillItemClaim,
) -> bool:
    """Heartbeat both durable leases while fetch/model I/O is in flight."""

    now = utc_now()
    active_run = await session.scalar(
        select(SiteMetadataBackfillRun.id).where(
            SiteMetadataBackfillRun.user_id == lease.user_id,
            SiteMetadataBackfillRun.id == lease.run_id,
            SiteMetadataBackfillRun.state == "running",
            SiteMetadataBackfillRun.stop_requested.is_(False),
            SiteMetadataBackfillRun.lease_token_hash == lease.token_hash,
            SiteMetadataBackfillRun.lease_expires_at >= now,
        )
    )
    if active_run is None:
        await session.rollback()
        return False
    if not await _renew_run_lease(session, lease, now=now):
        await session.rollback()
        return False
    renewed = await session.execute(
        update(SiteMetadataBackfillItem)
        .where(
            SiteMetadataBackfillItem.id == item.id,
            SiteMetadataBackfillItem.user_id == lease.user_id,
            SiteMetadataBackfillItem.run_id == lease.run_id,
            SiteMetadataBackfillItem.state == "running",
            SiteMetadataBackfillItem.lease_token_hash == item.token_hash,
            SiteMetadataBackfillItem.attempt_count == item.attempt_count,
            SiteMetadataBackfillItem.lease_expires_at >= now,
        )
        .values(
            lease_expires_at=now + ITEM_LEASE_DURATION,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if renewed.rowcount != 1:  # type: ignore[attr-defined]
        await session.rollback()
        return False
    await session.commit()
    return True


async def defer_item(
    session: AsyncSession,
    lease: MetadataBackfillRunLease,
    item: MetadataBackfillItemClaim,
) -> bool:
    """Return a fresh-claim conflict to the queue without counting a skip."""

    return await _return_or_fail_item(
        session,
        lease,
        item,
        preserve_analysis_claim=False,
        defer=True,
    )


async def _return_or_fail_item(
    session: AsyncSession,
    lease: MetadataBackfillRunLease,
    item: MetadataBackfillItemClaim,
    *,
    preserve_analysis_claim: bool,
    defer: bool,
) -> bool:
    """Release one claim without racing a persisted Provider fuse."""

    now = utc_now()
    stop_requested = await _lock_run_stop_state(session, lease)
    if stop_requested is None:
        await session.rollback()
        return False
    analysis_claimed_at = await session.scalar(
        select(SiteMetadataBackfillItem.analysis_claimed_at).where(
            SiteMetadataBackfillItem.id == item.id,
            SiteMetadataBackfillItem.user_id == lease.user_id,
            SiteMetadataBackfillItem.run_id == lease.run_id,
            SiteMetadataBackfillItem.state == "running",
            SiteMetadataBackfillItem.lease_token_hash == item.token_hash,
            SiteMetadataBackfillItem.attempt_count == item.attempt_count,
        )
    )
    transitioned = await session.execute(
        update(SiteMetadataBackfillItem)
        .where(
            SiteMetadataBackfillItem.id == item.id,
            SiteMetadataBackfillItem.user_id == lease.user_id,
            SiteMetadataBackfillItem.run_id == lease.run_id,
            SiteMetadataBackfillItem.state == "running",
            SiteMetadataBackfillItem.lease_token_hash == item.token_hash,
            SiteMetadataBackfillItem.attempt_count == item.attempt_count,
        )
        .values(
            state="failed" if stop_requested else "queued",
            analysis_claimed_at=(
                None
                if stop_requested or not preserve_analysis_claim
                else SiteMetadataBackfillItem.analysis_claimed_at
            ),
            lease_token_hash=None,
            lease_expires_at=None,
            available_at=(
                None
                if stop_requested or not defer
                else now + ITEM_DEFER_DURATION
            ),
            completed_at=now if stop_requested else None,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if transitioned.rowcount != 1:  # type: ignore[attr-defined]
        await session.rollback()
        return False
    if not await _adjust_run_counts(
        session,
        lease,
        running_delta=-1,
        **({"failed_delta": 1} if stop_requested else {"queued_delta": 1}),
    ):
        await session.rollback()
        return False
    if stop_requested and analysis_claimed_at is not None:
        await session.execute(
            update(Site)
            .where(
                Site.user_id == lease.user_id,
                Site.id == item.site_id,
                Site.analysis_status == "pending",
                Site.analysis_updated_at == analysis_claimed_at,
            )
            .values(
                analysis_status="not_analyzed",
                analysis_updated_at=now,
                updated_at=Site.updated_at,
            )
        )
    if stop_requested:
        await _finish_run_if_terminal(session, lease, now=now)
    await session.commit()
    return True


async def _finish_run_if_terminal(
    session: AsyncSession,
    lease: MetadataBackfillRunLease,
    *,
    now: datetime,
) -> None:
    """Close a run from its counters after the final item transition.

    The `queued_count == running_count == 0` predicate avoids an item-table
    probe on every completion.  It also makes concurrent consumers harmless:
    only the transaction that brings the final running count to zero can close
    the run.
    """

    await session.execute(
        update(SiteMetadataBackfillRun)
        .where(
            SiteMetadataBackfillRun.user_id == lease.user_id,
            SiteMetadataBackfillRun.id == lease.run_id,
            SiteMetadataBackfillRun.state == "running",
            SiteMetadataBackfillRun.lease_token_hash == lease.token_hash,
            SiteMetadataBackfillRun.queued_count == 0,
            SiteMetadataBackfillRun.running_count == 0,
        )
        .values(
            state=case(
                (SiteMetadataBackfillRun.failed_count > 0, "completed_with_errors"),
                else_="completed",
            ),
            lease_token_hash=None,
            lease_expires_at=None,
            heartbeat_at=now,
            completed_at=now,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )


async def _fail_queued_items_locked(
    session: AsyncSession,
    lease: MetadataBackfillRunLease,
    *,
    now: datetime,
) -> int | None:
    """Fail every unleased item inside the caller's fuse transaction."""

    abandoned_claims = [
        (str(site_id), claimed_at)
        for site_id, claimed_at in (
            await session.execute(
                select(
                    SiteMetadataBackfillItem.site_id,
                    SiteMetadataBackfillItem.analysis_claimed_at,
                ).where(
                    SiteMetadataBackfillItem.user_id == lease.user_id,
                    SiteMetadataBackfillItem.run_id == lease.run_id,
                    SiteMetadataBackfillItem.state == "queued",
                    SiteMetadataBackfillItem.analysis_claimed_at.is_not(None),
                )
            )
        ).all()
        if claimed_at is not None
    ]
    failed = await session.execute(
        update(SiteMetadataBackfillItem)
        .where(
            SiteMetadataBackfillItem.user_id == lease.user_id,
            SiteMetadataBackfillItem.run_id == lease.run_id,
            SiteMetadataBackfillItem.state == "queued",
        )
        .values(
            state="failed",
            analysis_claimed_at=None,
            lease_token_hash=None,
            lease_expires_at=None,
            available_at=None,
            completed_at=now,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    failed_count = int(failed.rowcount or 0)  # type: ignore[attr-defined]
    for site_id, claimed_at in abandoned_claims:
        await session.execute(
            update(Site)
            .where(
                Site.user_id == lease.user_id,
                Site.id == site_id,
                Site.analysis_status == "pending",
                Site.analysis_updated_at == claimed_at,
            )
            .values(
                analysis_status="not_analyzed",
                analysis_updated_at=now,
                updated_at=Site.updated_at,
            )
        )
    if failed_count:
        adjusted = await _adjust_run_counts(
            session,
            lease,
            queued_delta=-failed_count,
            failed_delta=failed_count,
        )
        if not adjusted:
            return None
    await _finish_run_if_terminal(session, lease, now=now)
    return failed_count


async def record_provider_result(
    session: AsyncSession,
    lease: MetadataBackfillRunLease,
    item: MetadataBackfillItemClaim,
    *,
    failed: bool | None,
    stop_batch: bool = False,
) -> bool | None:
    """Persist one model signal and return whether the run fuse is now set.

    ``None`` means ownership or the run fuse changed before this result could be
    recorded. A fatal signal stops immediately. Retryable failures add a bounded
    cooldown; one success clears the streak, and the third consecutive failure
    atomically stops all queued work.
    """

    if failed is None and not stop_batch:
        raise ValueError("a Provider signal must contain a result or stop request")

    now = utc_now()
    current_item = select(SiteMetadataBackfillItem.id).where(
        SiteMetadataBackfillItem.id == item.id,
        SiteMetadataBackfillItem.user_id == lease.user_id,
        SiteMetadataBackfillItem.run_id == lease.run_id,
        SiteMetadataBackfillItem.state == "running",
        SiteMetadataBackfillItem.lease_token_hash == item.token_hash,
        SiteMetadataBackfillItem.attempt_count == item.attempt_count,
        SiteMetadataBackfillItem.lease_expires_at >= now,
    ).exists()
    values: dict[str, object]
    if stop_batch:
        values = {
            "stop_requested": True,
            "provider_retry_at": None,
            "updated_at": now,
        }
    elif failed:
        values = {
            "consecutive_provider_failures": (
                SiteMetadataBackfillRun.consecutive_provider_failures + 1
            ),
            "updated_at": now,
        }
    else:
        values = {
            "consecutive_provider_failures": 0,
            "provider_retry_at": None,
            "updated_at": now,
        }
    recorded = await session.execute(
        update(SiteMetadataBackfillRun)
        .where(
            SiteMetadataBackfillRun.user_id == lease.user_id,
            SiteMetadataBackfillRun.id == lease.run_id,
            SiteMetadataBackfillRun.state == "running",
            SiteMetadataBackfillRun.stop_requested.is_(False),
            SiteMetadataBackfillRun.lease_token_hash == lease.token_hash,
            SiteMetadataBackfillRun.lease_expires_at >= now,
            current_item,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if recorded.rowcount != 1:  # type: ignore[attr-defined]
        await session.rollback()
        return None

    stop_requested = stop_batch
    if failed and not stop_batch:
        failure_count = int(
            await session.scalar(
                select(SiteMetadataBackfillRun.consecutive_provider_failures).where(
                    SiteMetadataBackfillRun.user_id == lease.user_id,
                    SiteMetadataBackfillRun.id == lease.run_id,
                    SiteMetadataBackfillRun.state == "running",
                    SiteMetadataBackfillRun.lease_token_hash == lease.token_hash,
                )
            )
            or 0
        )
        stop_requested = failure_count >= MAX_CONSECUTIVE_PROVIDER_FAILURES
        cooldown_seconds = min(
            PROVIDER_RETRY_BASE_SECONDS * (3 ** max(failure_count - 1, 0)),
            PROVIDER_RETRY_MAX_SECONDS,
        )
        cooled_down = await session.execute(
            update(SiteMetadataBackfillRun)
            .where(
                SiteMetadataBackfillRun.user_id == lease.user_id,
                SiteMetadataBackfillRun.id == lease.run_id,
                SiteMetadataBackfillRun.state == "running",
                SiteMetadataBackfillRun.lease_token_hash == lease.token_hash,
            )
            .values(
                stop_requested=stop_requested,
                provider_retry_at=now + timedelta(seconds=cooldown_seconds),
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if cooled_down.rowcount != 1:  # type: ignore[attr-defined]
            await session.rollback()
            return None
    if stop_requested:
        failed_count = await _fail_queued_items_locked(session, lease, now=now)
        if failed_count is None:
            await session.rollback()
            return None
    await session.commit()
    return stop_requested


async def finish_item(
    session: AsyncSession,
    lease: MetadataBackfillRunLease,
    item: MetadataBackfillItemClaim,
    *,
    state: str,
) -> bool:
    """Record one terminal outcome and atomically close a finished run."""

    if state not in _ITEM_TERMINAL_STATES:
        raise ValueError(f"unsupported metadata backfill item state: {state}")
    now = utc_now()
    stop_requested = await _lock_run_stop_state(session, lease)
    if stop_requested is None:
        await session.rollback()
        return False
    final_state = "failed" if stop_requested else state
    analysis_claimed_at = None
    if stop_requested:
        analysis_claimed_at = await session.scalar(
            select(SiteMetadataBackfillItem.analysis_claimed_at).where(
                SiteMetadataBackfillItem.id == item.id,
                SiteMetadataBackfillItem.user_id == lease.user_id,
                SiteMetadataBackfillItem.run_id == lease.run_id,
                SiteMetadataBackfillItem.state == "running",
                SiteMetadataBackfillItem.lease_token_hash == item.token_hash,
                SiteMetadataBackfillItem.attempt_count == item.attempt_count,
            )
        )
    completed = await session.execute(
        update(SiteMetadataBackfillItem)
        .where(
            SiteMetadataBackfillItem.id == item.id,
            SiteMetadataBackfillItem.user_id == lease.user_id,
            SiteMetadataBackfillItem.run_id == lease.run_id,
            SiteMetadataBackfillItem.state == "running",
            SiteMetadataBackfillItem.lease_token_hash == item.token_hash,
            SiteMetadataBackfillItem.attempt_count == item.attempt_count,
        )
        .values(
            state=final_state,
            analysis_claimed_at=None,
            lease_token_hash=None,
            lease_expires_at=None,
            available_at=None,
            completed_at=now,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if completed.rowcount != 1:  # type: ignore[attr-defined]
        await session.rollback()
        return False
    outcome_counter = {
        "complete": "complete_delta",
        "limited": "limited_delta",
        "failed": "failed_delta",
        "skipped": "skipped_delta",
    }[final_state]
    if not await _adjust_run_counts(
        session,
        lease,
        running_delta=-1,
        **{outcome_counter: 1},
    ):
        await session.rollback()
        return False
    if stop_requested and analysis_claimed_at is not None:
        await session.execute(
            update(Site)
            .where(
                Site.user_id == lease.user_id,
                Site.id == item.site_id,
                Site.analysis_status == "pending",
                Site.analysis_updated_at == analysis_claimed_at,
            )
            .values(
                analysis_status="not_analyzed",
                analysis_updated_at=now,
                updated_at=Site.updated_at,
            )
        )
    await _finish_run_if_terminal(session, lease, now=now)
    await session.commit()
    return True


async def release_item(
    session: AsyncSession,
    lease: MetadataBackfillRunLease,
    item: MetadataBackfillItemClaim,
) -> None:
    """Return a cancelled task, or fail it when the persisted fuse is set."""

    await _return_or_fail_item(
        session,
        lease,
        item,
        preserve_analysis_claim=True,
        defer=False,
    )


async def release_run_lease(
    session: AsyncSession,
    lease: MetadataBackfillRunLease,
) -> None:
    """Yield a coordinator lease on a controlled shutdown or cancellation.

    A process restart should not have to wait for a stale lease timer.  Every
    update is token-scoped, so an old process that wakes after another process
    took ownership cannot disturb the newer coordinator.
    """

    now = utc_now()
    stop_requested = await _lock_run_stop_state(session, lease)
    if stop_requested is None:
        await session.rollback()
        return
    if stop_requested:
        failed_queued_count = await _fail_queued_items_locked(session, lease, now=now)
        if failed_queued_count is None:
            await session.rollback()
            return
    running_claims: list[tuple[str, datetime]] = []
    if stop_requested:
        running_claims = [
            (str(site_id), claimed_at)
            for site_id, claimed_at in (
                await session.execute(
                    select(
                        SiteMetadataBackfillItem.site_id,
                        SiteMetadataBackfillItem.analysis_claimed_at,
                    ).where(
                        SiteMetadataBackfillItem.user_id == lease.user_id,
                        SiteMetadataBackfillItem.run_id == lease.run_id,
                        SiteMetadataBackfillItem.state == "running",
                        SiteMetadataBackfillItem.lease_token_hash == lease.token_hash,
                    )
                )
            ).all()
            if claimed_at is not None
        ]
    released_items = await session.execute(
        update(SiteMetadataBackfillItem)
        .where(
            SiteMetadataBackfillItem.user_id == lease.user_id,
            SiteMetadataBackfillItem.run_id == lease.run_id,
            SiteMetadataBackfillItem.state == "running",
            SiteMetadataBackfillItem.lease_token_hash == lease.token_hash,
        )
        .values(
            state="failed" if stop_requested else "queued",
            analysis_claimed_at=(
                None
                if stop_requested
                else SiteMetadataBackfillItem.analysis_claimed_at
            ),
            lease_token_hash=None,
            lease_expires_at=None,
            available_at=None,
            completed_at=now if stop_requested else None,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    released_count = int(released_items.rowcount or 0)  # type: ignore[attr-defined]
    if released_count and not await _adjust_run_counts(
        session,
        lease,
        running_delta=-released_count,
        **(
            {"failed_delta": released_count}
            if stop_requested
            else {"queued_delta": released_count}
        ),
    ):
        await session.rollback()
        return
    if stop_requested:
        for site_id, claimed_at in running_claims:
            await session.execute(
                update(Site)
                .where(
                    Site.user_id == lease.user_id,
                    Site.id == site_id,
                    Site.analysis_status == "pending",
                    Site.analysis_updated_at == claimed_at,
                )
                .values(
                    analysis_status="not_analyzed",
                    analysis_updated_at=now,
                    updated_at=Site.updated_at,
                )
            )
        await _finish_run_if_terminal(session, lease, now=now)
    else:
        await session.execute(
            update(SiteMetadataBackfillRun)
            .where(
                SiteMetadataBackfillRun.user_id == lease.user_id,
                SiteMetadataBackfillRun.id == lease.run_id,
                SiteMetadataBackfillRun.state == "running",
                SiteMetadataBackfillRun.lease_token_hash == lease.token_hash,
            )
            .values(
                state="queued",
                lease_token_hash=None,
                lease_expires_at=None,
                heartbeat_at=now,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
    await session.commit()


__all__ = [
    "MetadataBackfillItemClaim",
    "MetadataBackfillProgress",
    "MetadataBackfillRunLease",
    "MetadataBackfillStart",
    "ITEM_LEASE_HEARTBEAT_SECONDS",
    "active_metadata_backfill",
    "acquire_run_lease",
    "claim_next_item",
    "defer_item",
    "finish_item",
    "item_execution_intent",
    "lease_retry_delay",
    "list_active_runs",
    "progress_for_run",
    "record_item_site_claim",
    "record_provider_result",
    "renew_item_lease",
    "release_item",
    "release_run_lease",
    "start_metadata_backfill",
]
