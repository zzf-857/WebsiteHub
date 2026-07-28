"""Apply a fetch outcome to one stored Site.

The rule that shapes everything here: **analysis fills blanks, it never
overwrites the user.**  A name or description the user typed is a decision;
a page's ``<title>`` is a guess about that decision.  Guesses lose.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.db.database import Database
from webhub.db.models import Site, utc_now

from .fetcher import DEFAULT_TIMEOUT_SECONDS, FetchOutcome, fetch_site_metadata

_LOGGER = logging.getLogger(__name__)

MAX_DESCRIPTION_CHARS = 1_000
AUTO_PENDING_STALE_AFTER = timedelta(minutes=5)
AUTO_PARTIAL_RETRY_AFTER = timedelta(minutes=30)


@dataclass(frozen=True, slots=True)
class AnalysisClaim:
    user_id: str
    site_id: str
    url: str
    version: int
    claimed_at: datetime
    partial_retry: bool = False
    missing_description: bool = False
    missing_icon: bool = False


_ACTIVE_CLAIMS: dict[tuple[str, str], datetime] = {}


def _claim_key(user_id: str, site_id: str) -> tuple[str, str]:
    return user_id, site_id


def _claim_is_current(claim: AnalysisClaim) -> bool:
    return _ACTIVE_CLAIMS.get(_claim_key(claim.user_id, claim.site_id)) == claim.claimed_at


def _release_claim(claim: AnalysisClaim) -> None:
    key = _claim_key(claim.user_id, claim.site_id)
    if _ACTIVE_CLAIMS.get(key) == claim.claimed_at:
        _ACTIVE_CLAIMS.pop(key, None)


def _blank(value: str | None) -> bool:
    return value is None or not value.strip()


def _partial_metadata_condition() -> object:
    """Sites whose basic library identity is still visibly incomplete."""

    return or_(
        Site.description.is_(None),
        func.trim(Site.description) == "",
        Site.favicon_url.is_(None),
        func.trim(Site.favicon_url) == "",
    )


def _automatic_partial_retry_condition(*, partial_before: datetime) -> object:
    """Retry derived blanks once, but never overwrite a later user decision."""

    return and_(
        Site.analysis_status == "complete",
        _partial_metadata_condition(),
        Site.analysis_updated_at.is_not(None),
        Site.analysis_updated_at < partial_before,
        # `analysis_updated_at` is derived-only. A later user edit, including
        # an intentional clear, must keep the record out of automatic retries.
        Site.updated_at <= Site.analysis_updated_at,
    )


def _automatic_eligibility(*, stale_before: datetime, partial_before: datetime) -> object:
    return or_(
        Site.analysis_status == "not_analyzed",
        and_(
            Site.analysis_status == "pending",
            or_(
                Site.analysis_updated_at.is_(None),
                Site.analysis_updated_at < stale_before,
            ),
        ),
        _automatic_partial_retry_condition(partial_before=partial_before),
    )


async def _owned_site(session: AsyncSession, user_id: str, site_id: str) -> Site | None:
    return await session.scalar(select(Site).where(Site.user_id == user_id, Site.id == site_id))


async def _claim_analysis(
    session: AsyncSession,
    user_id: str,
    site_id: str,
    *,
    automatic: bool = False,
    stale_before: datetime | None = None,
) -> AnalysisClaim | None:
    ownership = [Site.user_id == user_id, Site.id == site_id]
    if automatic:
        cutoff = stale_before or (utc_now() - AUTO_PENDING_STALE_AFTER)
        ownership.append(
            _automatic_eligibility(
                stale_before=cutoff,
                partial_before=utc_now() - AUTO_PARTIAL_RETRY_AFTER,
            )
        )
    site = await session.scalar(select(Site).where(*ownership))
    if site is None:
        return None

    key = _claim_key(user_id, site_id)
    previous = _ACTIVE_CLAIMS.get(key)
    claimed_at = utc_now()
    if previous is not None and claimed_at <= previous:
        claimed_at = previous + timedelta(microseconds=1)
    _ACTIVE_CLAIMS[key] = claimed_at
    try:
        claimed = await session.execute(
            update(Site)
            .where(
                *ownership,
                Site.original_url == site.original_url,
                Site.version == site.version,
            )
            .values(
                analysis_status="pending",
                analysis_updated_at=claimed_at,
                # ``updated_at`` has a Python onupdate default. Explicitly keep
                # it unchanged: derived analysis is not a user edit.
                updated_at=Site.updated_at,
            )
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:  # type: ignore[attr-defined]
            await session.rollback()
            if _ACTIVE_CLAIMS.get(key) == claimed_at:
                if previous is None:
                    _ACTIVE_CLAIMS.pop(key, None)
                else:
                    _ACTIVE_CLAIMS[key] = previous
            return None
        await session.commit()
    except BaseException:
        if _ACTIVE_CLAIMS.get(key) == claimed_at:
            if previous is None:
                _ACTIVE_CLAIMS.pop(key, None)
            else:
                _ACTIVE_CLAIMS[key] = previous
        raise
    return AnalysisClaim(
        user_id=user_id,
        site_id=site_id,
        url=site.original_url,
        version=site.version,
        claimed_at=claimed_at,
        partial_retry=automatic and site.analysis_status == "complete",
        missing_description=_blank(site.description),
        missing_icon=_blank(site.favicon_url),
    )


async def auto_backfill_site_ids(
    session: AsyncSession,
    user_id: str,
    *,
    limit: int,
    excluded_site_ids: frozenset[str] = frozenset(),
    stale_before: datetime | None = None,
) -> list[str]:
    """Discover fresh, abandoned, and once-retryable partial analysis work."""

    cutoff = stale_before or (utc_now() - AUTO_PENDING_STALE_AFTER)
    partial_cutoff = utc_now() - AUTO_PARTIAL_RETRY_AFTER
    selected: list[str] = []
    excluded = set(excluded_site_ids)

    async def extend(
        eligibility: object,
        *ordering: object,
    ) -> None:
        remaining = limit - len(selected)
        if remaining <= 0:
            return
        conditions = [Site.user_id == user_id, eligibility]
        if excluded:
            conditions.append(Site.id.not_in(excluded))
        site_ids = list(
            (
                await session.scalars(
                    select(Site.id)
                    .where(*conditions)
                    .order_by(*ordering)
                    .limit(remaining)
                )
            ).all()
        )
        selected.extend(site_ids)
        excluded.update(site_ids)

    # These are deliberately separate indexed queries rather than one
    # `OR` plus conditional ordering expression. A sweep over 10,000 old
    # bookmarks otherwise asks SQLite to scan and sort the remaining corpus for
    # every tiny discovery batch. The model index orders exactly these columns.
    await extend(
        Site.analysis_status == "not_analyzed",
        Site.analysis_updated_at,
        Site.created_at,
        Site.id,
    )
    await extend(
        and_(
            Site.analysis_status == "pending",
            Site.analysis_updated_at.is_(None),
        ),
        Site.created_at,
        Site.id,
    )
    await extend(
        and_(
            Site.analysis_status == "pending",
            Site.analysis_updated_at < cutoff,
        ),
        Site.analysis_updated_at,
        Site.created_at,
        Site.id,
    )
    await extend(
        _automatic_partial_retry_condition(partial_before=partial_cutoff),
        Site.analysis_updated_at,
        Site.created_at,
        Site.id,
    )
    return selected


async def recent_not_analyzed_site_ids(
    session: AsyncSession,
    user_id: str,
    *,
    limit: int,
) -> list[str]:
    """Return a tiny newest-first slice for a just-completed import.

    This is deliberately separate from the oldest-first background sweep.  A
    large historical library must make forward progress fairly, while the few
    links the user just imported should begin showing basic metadata promptly.
    """

    if limit <= 0:
        return []
    return list(
        (
            await session.scalars(
                select(Site.id)
                .where(
                    Site.user_id == user_id,
                    Site.analysis_status == "not_analyzed",
                )
                .order_by(Site.created_at.desc(), Site.id.desc())
                .limit(limit)
            )
        ).all()
    )


async def not_analyzed_site_ids(
    session: AsyncSession,
    user_id: str,
    *,
    limit: int,
    excluded_site_ids: frozenset[str] = frozenset(),
) -> tuple[list[str], int]:
    """Select manual retry work without stealing a fresh background claim."""

    conditions = [
        Site.user_id == user_id,
        Site.analysis_status.in_(("not_analyzed", "failed", "limited")),
    ]
    if excluded_site_ids:
        conditions.append(Site.id.not_in(excluded_site_ids))
    total = int(
        await session.scalar(select(func.count()).select_from(Site).where(*conditions)) or 0
    )
    site_ids = list(
        (
            await session.scalars(
                select(Site.id).where(*conditions).order_by(Site.created_at, Site.id).limit(limit)
            )
        ).all()
    )
    return site_ids, max(total - len(site_ids), 0)


def _safe_image_url(url: str | None) -> str | None:
    """Only http(s) URLs; both columns feed an ``img src``."""

    if not url:
        return None
    candidate = url.strip()
    if not candidate.lower().startswith(("http://", "https://")):
        return None
    return candidate[:2_048]


async def apply_outcome(
    session: AsyncSession,
    claim: AnalysisClaim,
    outcome: FetchOutcome,
) -> Site | None:
    """Atomically fill blanks when URL and user-visible version are unchanged."""

    if not _claim_is_current(claim):
        return await _owned_site(session, claim.user_id, claim.site_id)

    completed_at = utc_now()
    final_status = outcome.status
    values: dict[str, object] = {
        "analysis_status": final_status,
        "analysis_updated_at": completed_at,
        "updated_at": Site.updated_at,
    }
    metadata = outcome.metadata
    if metadata.description:
        values["description"] = case(
            (func.trim(Site.description) == "", metadata.description[:MAX_DESCRIPTION_CHARS]),
            else_=Site.description,
        )
    icon = _safe_image_url(metadata.icon_url)
    if icon:
        values["favicon_url"] = case(
            (
                or_(Site.favicon_url.is_(None), func.trim(Site.favicon_url) == ""),
                icon,
            ),
            else_=Site.favicon_url,
        )
    preview = _safe_image_url(metadata.image_url)
    if preview:
        values["preview_url"] = case(
            (
                or_(Site.preview_url.is_(None), func.trim(Site.preview_url) == ""),
                preview,
            ),
            else_=Site.preview_url,
        )

    # A first pass can reach a page while its favicon endpoint is briefly down.
    # Give a complete-but-incomplete record one delayed automatic retry. If the
    # retry still cannot fill the fields it was missing, make it terminal so a
    # library with thousands of links never circles forever on the same hosts.
    if claim.partial_retry and outcome.status == "complete":
        repaired_description = not claim.missing_description or bool(metadata.description)
        repaired_icon = not claim.missing_icon or icon is not None
        if not repaired_description or not repaired_icon:
            final_status = "limited"
            values["analysis_status"] = final_status

    # Do not load the row and mutate an ORM object here. Sessions deliberately
    # use expire_on_commit=False, so a fetch that took several seconds would be
    # writing decisions based on a stale identity-map snapshot. Every blank
    # check belongs in the UPDATE itself, where it observes the database's
    # current value atomically.
    applied = await session.execute(
        update(Site)
        .where(
            Site.user_id == claim.user_id,
            Site.id == claim.site_id,
            Site.original_url == claim.url,
            Site.version == claim.version,
            Site.analysis_status == "pending",
            Site.analysis_updated_at == claim.claimed_at,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )

    if applied.rowcount != 1 and _claim_is_current(claim):  # type: ignore[attr-defined]
        # Any user edit invalidates metadata from the in-flight fetch. This is
        # stricter than checking each field for blanks: explicitly clearing a
        # description or favicon is itself a user decision and must stay clear.
        # The same URL can still receive the terminal outcome; a changed URL
        # returns to `not_analyzed` because the outcome describes the old page.
        await session.execute(
            update(Site)
            .where(
                Site.user_id == claim.user_id,
                Site.id == claim.site_id,
                Site.analysis_status == "pending",
                Site.analysis_updated_at == claim.claimed_at,
            )
            .values(
                analysis_status=case(
                    (Site.original_url == claim.url, final_status),
                    else_="not_analyzed",
                ),
                analysis_updated_at=completed_at,
                updated_at=Site.updated_at,
            )
            .execution_options(synchronize_session=False)
        )

    # **不 bump version。** version 是给用户可见的并发编辑用的乐观锁；
    # 后台分析是系统侧的补白，涨版本号等于系统跟用户抢锁——用户刚建完网站
    # 马上编辑就会撞上「已被修改，请刷新后重试」，而"改动"是我们自己做的。
    # 分析与用户编辑并发时，version 条件会让本次补白整体作废，保证用户永远赢。
    await session.commit()
    return await session.scalar(
        select(Site)
        .where(Site.user_id == claim.user_id, Site.id == claim.site_id)
        .execution_options(populate_existing=True)
    )


async def _record_claim_terminal(
    session: AsyncSession,
    claim: AnalysisClaim,
    status: str,
) -> None:
    """End only the claim owned by this task; a newer run always wins."""

    await session.rollback()
    if not _claim_is_current(claim):
        return
    completed_at = utc_now()
    strict = await session.execute(
        update(Site)
        .where(
            Site.user_id == claim.user_id,
            Site.id == claim.site_id,
            Site.analysis_status == "pending",
            Site.analysis_updated_at == claim.claimed_at,
        )
        .values(
            analysis_status=case(
                (Site.original_url == claim.url, status),
                else_="not_analyzed",
            ),
            analysis_updated_at=completed_at,
            updated_at=Site.updated_at,
        )
        .execution_options(synchronize_session=False)
    )
    if strict.rowcount != 1 and _claim_is_current(claim):  # type: ignore[attr-defined]
        # A user edit changes the version or URL while analysis has its own
        # token. The in-process owner check still proves no newer run exists.
        await session.execute(
            update(Site)
            .where(
                Site.user_id == claim.user_id,
                Site.id == claim.site_id,
                Site.analysis_status == "pending",
                Site.analysis_updated_at == claim.claimed_at,
            )
            .values(
                analysis_status=case(
                    (Site.original_url == claim.url, status),
                    else_="not_analyzed",
                ),
                analysis_updated_at=completed_at,
                updated_at=Site.updated_at,
            )
            .execution_options(synchronize_session=False)
        )
    await session.commit()


async def analyze_site(
    session: AsyncSession,
    user_id: str,
    site_id: str,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    automatic: bool = False,
    stale_before: datetime | None = None,
) -> FetchOutcome | None:
    """Mark the site pending, fetch it, and store the result.

    Returns ``None`` when the site does not belong to this account — the same
    answer a missing site gives, so a probe cannot distinguish the two.
    """

    claim = await _claim_analysis(
        session,
        user_id,
        site_id,
        automatic=automatic,
        stale_before=stale_before,
    )
    if claim is None:
        return None

    try:
        outcome = await fetch_site_metadata(claim.url, timeout_seconds=timeout_seconds)
        await apply_outcome(session, claim, outcome)
        return outcome
    except asyncio.CancelledError:
        try:
            await _record_claim_terminal(session, claim, "not_analyzed")
        except Exception:  # noqa: BLE001 - cancellation cleanup is best effort and logged
            _LOGGER.exception("could not release cancelled analysis claim for %s", site_id)
        raise
    except Exception:
        try:
            await _record_claim_terminal(session, claim, "failed")
        except Exception:  # noqa: BLE001 - preserve the original error
            _LOGGER.exception("could not record analysis failure for %s", site_id)
        raise
    finally:
        _release_claim(claim)


async def analyze_in_background(
    database: Database,
    user_id: str,
    site_id: str,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    automatic: bool = False,
    stale_before: datetime | None = None,
) -> FetchOutcome | None:
    """Run one analysis detached from the request that triggered it.

    Saving a site must not wait on someone else's slow server, and must not
    fail because that server is down.
    """

    try:
        async with database.sessions() as session:
            return await analyze_site(
                session,
                user_id,
                site_id,
                timeout_seconds=timeout_seconds,
                automatic=automatic,
                stale_before=stale_before,
            )
    except asyncio.CancelledError:
        raise
    except Exception as error:  # noqa: BLE001 - a background failure must stay contained
        _LOGGER.warning("site analysis failed for %s", site_id, exc_info=error)
        return None


__all__ = [
    "analyze_in_background",
    "analyze_site",
    "AnalysisClaim",
    "AUTO_PENDING_STALE_AFTER",
    "AUTO_PARTIAL_RETRY_AFTER",
    "apply_outcome",
    "auto_backfill_site_ids",
    "not_analyzed_site_ids",
    "recent_not_analyzed_site_ids",
]
