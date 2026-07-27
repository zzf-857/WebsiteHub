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

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.db.database import Database
from webhub.db.models import Site, utc_now

from .fetcher import DEFAULT_TIMEOUT_SECONDS, FetchOutcome, fetch_site_metadata

_LOGGER = logging.getLogger(__name__)

MAX_DESCRIPTION_CHARS = 1_000


@dataclass(frozen=True, slots=True)
class AnalysisClaim:
    user_id: str
    site_id: str
    url: str
    version: int
    claimed_at: datetime


_ACTIVE_CLAIMS: dict[tuple[str, str], datetime] = {}


def _claim_key(user_id: str, site_id: str) -> tuple[str, str]:
    return user_id, site_id


def _claim_is_current(claim: AnalysisClaim) -> bool:
    return _ACTIVE_CLAIMS.get(_claim_key(claim.user_id, claim.site_id)) == claim.claimed_at


def _release_claim(claim: AnalysisClaim) -> None:
    key = _claim_key(claim.user_id, claim.site_id)
    if _ACTIVE_CLAIMS.get(key) == claim.claimed_at:
        _ACTIVE_CLAIMS.pop(key, None)


async def _owned_site(session: AsyncSession, user_id: str, site_id: str) -> Site | None:
    return await session.scalar(select(Site).where(Site.user_id == user_id, Site.id == site_id))


async def _claim_analysis(
    session: AsyncSession,
    user_id: str,
    site_id: str,
) -> AnalysisClaim | None:
    site = await _owned_site(session, user_id, site_id)
    if site is None:
        return None

    key = _claim_key(user_id, site_id)
    previous = _ACTIVE_CLAIMS.get(key)
    claimed_at = utc_now()
    if previous is not None and claimed_at <= previous:
        claimed_at = previous + timedelta(microseconds=1)
    _ACTIVE_CLAIMS[key] = claimed_at
    try:
        site.analysis_status = "pending"
        site.updated_at = claimed_at
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
    )


async def not_analyzed_site_ids(
    session: AsyncSession,
    user_id: str,
    *,
    limit: int,
    excluded_site_ids: frozenset[str] = frozenset(),
) -> tuple[list[str], int]:
    """Select incomplete or retryable work without re-queueing active tasks."""

    conditions = [
        Site.user_id == user_id,
        Site.analysis_status.in_(("not_analyzed", "pending", "failed", "limited")),
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
    values: dict[str, object] = {
        "analysis_status": outcome.status,
        "updated_at": completed_at,
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
            Site.updated_at == claim.claimed_at,
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
            )
            .values(
                analysis_status=case(
                    (Site.original_url == claim.url, outcome.status),
                    else_="not_analyzed",
                ),
                updated_at=completed_at,
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
            Site.updated_at == claim.claimed_at,
        )
        .values(
            analysis_status=case(
                (Site.original_url == claim.url, status),
                else_="not_analyzed",
            ),
            updated_at=completed_at,
        )
        .execution_options(synchronize_session=False)
    )
    if strict.rowcount != 1 and _claim_is_current(claim):  # type: ignore[attr-defined]
        # A user edit updates `updated_at` and invalidates the strict database
        # token. The in-process owner check still proves no newer run exists.
        await session.execute(
            update(Site)
            .where(
                Site.user_id == claim.user_id,
                Site.id == claim.site_id,
                Site.analysis_status == "pending",
            )
            .values(
                analysis_status=case(
                    (Site.original_url == claim.url, status),
                    else_="not_analyzed",
                ),
                updated_at=completed_at,
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
) -> FetchOutcome | None:
    """Mark the site pending, fetch it, and store the result.

    Returns ``None`` when the site does not belong to this account — the same
    answer a missing site gives, so a probe cannot distinguish the two.
    """

    claim = await _claim_analysis(session, user_id, site_id)
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
) -> FetchOutcome | None:
    """Run one analysis detached from the request that triggered it.

    Saving a site must not wait on someone else's slow server, and must not
    fail because that server is down.
    """

    try:
        async with database.sessions() as session:
            return await analyze_site(session, user_id, site_id, timeout_seconds=timeout_seconds)
    except asyncio.CancelledError:
        raise
    except Exception as error:  # noqa: BLE001 - a background failure must stay contained
        _LOGGER.warning("site analysis failed for %s", site_id, exc_info=error)
        return None


__all__ = [
    "analyze_in_background",
    "analyze_site",
    "AnalysisClaim",
    "apply_outcome",
    "not_analyzed_site_ids",
]
