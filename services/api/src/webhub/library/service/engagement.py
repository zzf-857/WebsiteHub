"""Usage signals, resurfacing and the library overview (Q42 收藏模型).

Two rules hold for everything in this module:

* **Usage is not editing.** Opening, starring, marking seen, snoozing and
  muting never change ``Site.version`` or ``Site.updated_at``. ``updated_at``
  carries an ORM ``onupdate``; every write here is one explicit SQL UPDATE that
  assigns ``updated_at=Site.updated_at`` so the hook cannot fire. A stale edit
  form therefore never conflicts with a click, and "recently updated" ordering
  is not reshuffled by browsing.
* **Reads are reads.** ``rediscover`` and ``library_overview`` issue SELECTs
  only. The daily rotation is derived from a hash of (account, UTC date,
  shuffle), not from stored state and not from ``ORDER BY random()`` over the
  whole table.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import ColumnElement, and_, case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.db.models import Category, Site, SiteTag, Tag, utc_now
from webhub.library.kinds import SITE_KINDS
from webhub.library.schemas import (
    MAX_REDISCOVER_LIMIT,
    LibraryKindCounts,
    LibraryOverviewResponse,
    RediscoverItemResponse,
    RediscoverResponse,
    SiteOpenResponse,
    SiteResponse,
    SiteStateUpdateRequest,
)

from ._common import LibraryNotFoundError, _never_opened, _state_condition
from .sites import _site_response

# A save younger than this is still "fresh in mind" and is never resurfaced.
REDISCOVER_MIN_AGE = timedelta(days=3)
# Recent interests: the most used tags among sites saved in this window.
INTEREST_WINDOW = timedelta(days=30)
INTEREST_TAG_LIMIT = 5
# A tag on a single recent save is not yet a pattern ("常收藏").
INTEREST_TAG_MIN_SITES = 2
REASON_TAG_LIMIT = 2
OVERVIEW_WINDOW = timedelta(days=7)


def _as_utc(value: datetime) -> datetime:
    # SQLite returns naive UTC wall-clock values; freshly written ORM objects
    # still hold aware ones. Normalize before any arithmetic.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _usage_update(user_id: str, site_id: str, values: dict[str, object]):
    """One account-scoped UPDATE that leaves ``version`` and ``updated_at`` alone."""

    return (
        update(Site)
        .where(Site.user_id == user_id, Site.id == site_id)
        # Self-assignment suppresses the ORM ``onupdate=utc_now`` hook; the
        # version column is deliberately never part of ``values``.
        .values(**values, updated_at=Site.updated_at)
        .execution_options(synchronize_session=False)
    )


async def record_site_open(
    session: AsyncSession,
    user_id: str,
    site_id: str,
    *,
    now: datetime | None = None,
) -> SiteOpenResponse:
    """Count one open through WebHub; the first open also marks the site seen."""

    opened_at = now or utc_now()
    row = (
        await session.execute(
            _usage_update(
                user_id,
                site_id,
                {
                    "open_count": Site.open_count + 1,
                    "last_opened_at": opened_at,
                    "seen_at": func.coalesce(Site.seen_at, opened_at),
                },
            ).returning(Site.id, Site.open_count, Site.last_opened_at, Site.seen_at)
        )
    ).one_or_none()
    if row is None:
        await session.rollback()
        raise LibraryNotFoundError("网站不存在")
    await session.commit()
    return SiteOpenResponse(
        site_id=row.id,
        open_count=row.open_count,
        last_opened_at=row.last_opened_at,
        seen_at=row.seen_at,
    )


async def update_site_state(
    session: AsyncSession,
    user_id: str,
    site_id: str,
    payload: SiteStateUpdateRequest,
    *,
    now: datetime | None = None,
) -> SiteResponse:
    """Apply reversible engagement state; repeating a value is idempotent."""

    changed_at = now or utc_now()
    fields = payload.model_fields_set
    values: dict[str, object] = {}
    if "starred" in fields:
        # Re-starring keeps the original star time.
        values["starred_at"] = (
            func.coalesce(Site.starred_at, changed_at) if payload.starred else None
        )
    if "seen" in fields:
        values["seen_at"] = func.coalesce(Site.seen_at, changed_at) if payload.seen else None
    if "snooze_days" in fields:
        days = int(payload.snooze_days or 0)
        if days == 0:
            values["resurface_due_at"] = None
        else:
            values["resurface_due_at"] = changed_at + timedelta(days=days)
            values["resurface_count"] = Site.resurface_count + 1
    if "resurface_muted" in fields:
        values["resurface_muted"] = bool(payload.resurface_muted)
    result = await session.execute(_usage_update(user_id, site_id, values))
    if result.rowcount != 1:  # type: ignore[attr-defined]
        await session.rollback()
        raise LibraryNotFoundError("网站不存在")
    await session.commit()
    site = await session.scalar(
        select(Site)
        .where(Site.user_id == user_id, Site.id == site_id)
        # The bulk UPDATE bypassed the identity map; never answer from it.
        .execution_options(populate_existing=True)
    )
    if site is None:
        raise LibraryNotFoundError("网站不存在")
    return await _site_response(session, user_id, site)


def rediscover_pivot(user_id: str, day: date, shuffle: int) -> str:
    """Deterministic id pivot for one account, UTC day and shuffle round.

    Site ids are uuid4 strings, so the first hex characters of a hash split
    them at a uniformly distributed point. Candidates are read from the pivot
    upwards and wrap around, which rotates the daily selection with plain
    keyset reads on the ``(user_id, last_opened_at, id)`` index.
    """

    digest = hashlib.sha256(f"{user_id}:{day.isoformat()}:{shuffle}".encode()).hexdigest()
    return digest[:8]


def _rediscover_conditions(user_id: str, now: datetime) -> list[ColumnElement[bool]]:
    return [
        Site.user_id == user_id,
        _never_opened(),
        Site.seen_at.is_(None),
        Site.resurface_muted.is_(False),
        or_(Site.resurface_due_at.is_(None), Site.resurface_due_at <= now),
        Site.created_at <= now - REDISCOVER_MIN_AGE,
    ]


async def _interest_tags(
    session: AsyncSession,
    user_id: str,
    now: datetime,
) -> list[tuple[str, str]]:
    """The account's most used tags among saves of the last 30 days."""

    uses = func.count(SiteTag.site_id)
    rows = (
        await session.execute(
            select(Tag.id, Tag.name)
            .join(SiteTag, and_(SiteTag.user_id == Tag.user_id, SiteTag.tag_id == Tag.id))
            .join(Site, and_(Site.user_id == SiteTag.user_id, Site.id == SiteTag.site_id))
            .where(Tag.user_id == user_id, Site.created_at >= now - INTEREST_WINDOW)
            .group_by(Tag.id, Tag.name, Tag.normalized_name)
            .having(uses >= INTEREST_TAG_MIN_SITES)
            .order_by(uses.desc(), Tag.normalized_name, Tag.id)
            .limit(INTEREST_TAG_LIMIT)
        )
    ).all()
    return [(str(tag_id), str(name)) for tag_id, name in rows]


async def _rotated_sites(
    session: AsyncSession,
    conditions: list[ColumnElement[bool]],
    *,
    pivot: str,
    limit: int,
    exclude_ids: list[str],
) -> list[Site]:
    """Up to ``limit`` candidates from ``pivot`` upwards, wrapping around."""

    if limit <= 0:
        return []
    base = [*conditions]
    if exclude_ids:
        base.append(Site.id.not_in(exclude_ids))
    upper = list(
        (
            await session.scalars(
                select(Site).where(*base, Site.id >= pivot).order_by(Site.id).limit(limit)
            )
        ).all()
    )
    if len(upper) >= limit:
        return upper
    lower = list(
        (
            await session.scalars(
                select(Site)
                .where(*base, Site.id < pivot)
                .order_by(Site.id)
                .limit(limit - len(upper))
            )
        ).all()
    )
    return [*upper, *lower]


async def _site_cards(
    session: AsyncSession,
    user_id: str,
    sites: list[Site],
) -> list[SiteResponse]:
    """Full ``SiteResponse`` cards for a few sites in two account-scoped reads."""

    if not sites:
        return []
    site_ids = [site.id for site in sites]
    categories = {
        category.id: category
        for category in (
            await session.scalars(
                select(Category).where(
                    Category.user_id == user_id,
                    Category.id.in_({site.category_id for site in sites}),
                )
            )
        ).all()
    }
    tags_by_site: dict[str, list[Tag]] = {site_id: [] for site_id in site_ids}
    tag_rows = (
        await session.execute(
            select(SiteTag.site_id, Tag)
            .join(Tag, and_(Tag.user_id == SiteTag.user_id, Tag.id == SiteTag.tag_id))
            .where(SiteTag.user_id == user_id, SiteTag.site_id.in_(site_ids))
            .order_by(SiteTag.site_id, Tag.normalized_name, Tag.id)
        )
    ).all()
    for site_id, tag in tag_rows:
        tags_by_site[site_id].append(tag)
    # ``_site_response`` falls back to its own owned-category read only if a
    # category vanished between the two queries (it then raises not-found).
    return [
        await _site_response(
            session,
            user_id,
            site,
            categories.get(site.category_id),
            tags_by_site[site.id],
        )
        for site in sites
    ]


def rediscover_reason(*, saved_days: int, matched_tags: list[str], resurface_count: int) -> str:
    """Rule-generated, explainable reason; never model-generated."""

    parts = [f"{saved_days} 天前收藏，一次都没打开过"]
    if matched_tags:
        quoted = "".join(f"「{name}」" for name in matched_tags[:REASON_TAG_LIMIT])
        parts.append(f"和你最近常收藏的{quoted}相关")
    elif resurface_count > 0:
        parts.append(f"推迟过 {resurface_count} 次，提醒已到期")
    return "；".join(parts)


async def rediscover(
    session: AsyncSession,
    user_id: str,
    *,
    limit: int,
    shuffle: int,
    now: datetime | None = None,
) -> RediscoverResponse:
    """Pick saved-but-never-opened sites to bring back today.

    Candidates: never opened, not marked seen, not muted, due (no snooze or the
    snooze has passed) and saved at least three days ago. Order: overlap with
    recent interest tags first, then the deterministic daily rotation.
    """

    current = now or utc_now()
    today = _as_utc(current).date()
    bounded_limit = max(1, min(limit, MAX_REDISCOVER_LIMIT))
    conditions = _rediscover_conditions(user_id, current)
    pivot = rediscover_pivot(user_id, today, shuffle)

    interest = await _interest_tags(session, user_id, current)
    interest_rank = {tag_id: rank for rank, (tag_id, _) in enumerate(interest)}
    picked: list[Site] = []
    if interest_rank:
        overlap = (
            select(SiteTag.site_id.label("site_id"), func.count().label("overlap"))
            .where(SiteTag.user_id == user_id, SiteTag.tag_id.in_(list(interest_rank)))
            .group_by(SiteTag.site_id)
            .subquery()
        )
        # Same rotation inside each overlap tier: ids from the pivot up first.
        wrap_order = case((Site.id >= pivot, 0), else_=1)
        picked = list(
            (
                await session.scalars(
                    select(Site)
                    .join(overlap, overlap.c.site_id == Site.id)
                    .where(*conditions)
                    .order_by(overlap.c.overlap.desc(), wrap_order, Site.id)
                    .limit(bounded_limit)
                )
            ).all()
        )
    # Fewer overlapping candidates than requested means all of them were read
    # above, so excluding exactly those ids leaves the non-overlapping rest.
    if len(picked) < bounded_limit:
        picked.extend(
            await _rotated_sites(
                session,
                conditions,
                pivot=pivot,
                limit=bounded_limit - len(picked),
                exclude_ids=[site.id for site in picked],
            )
        )

    items: list[RediscoverItemResponse] = []
    for site, response in zip(picked, await _site_cards(session, user_id, picked), strict=True):
        matched = sorted(
            (tag for tag in response.tags if tag.id in interest_rank),
            key=lambda tag: interest_rank[tag.id],
        )
        saved_days = max(0, (_as_utc(current) - _as_utc(site.created_at)).days)
        items.append(
            RediscoverItemResponse(
                site=response,
                saved_days=saved_days,
                reason=rediscover_reason(
                    saved_days=saved_days,
                    matched_tags=[tag.name for tag in matched],
                    resurface_count=site.resurface_count,
                ),
            )
        )
    return RediscoverResponse(generated_for=today, items=items)


async def library_overview(
    session: AsyncSession,
    user_id: str,
    *,
    now: datetime | None = None,
) -> LibraryOverviewResponse:
    """Account totals in one grouped scan; "opened" counts distinct sites."""

    window_start = (now or utc_now()) - OVERVIEW_WINDOW

    def tally(condition: ColumnElement[bool]):
        return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)

    rows = (
        await session.execute(
            select(
                Site.kind,
                func.count(Site.id),
                tally(_state_condition("unopened")),
                tally(_state_condition("starred")),
                tally(Site.created_at >= window_start),
                tally(Site.last_opened_at >= window_start),
            )
            .where(Site.user_id == user_id)
            .group_by(Site.kind)
        )
    ).all()
    by_kind = dict.fromkeys(SITE_KINDS, 0)
    total = unopened = starred = added = opened = 0
    for kind, count, kind_unopened, kind_starred, kind_added, kind_opened in rows:
        # The CHECK constraint keeps ``kind`` inside SITE_KINDS, so the
        # per-kind counts always add up to ``total``.
        by_kind[kind] += int(count)
        total += int(count)
        unopened += int(kind_unopened)
        starred += int(kind_starred)
        added += int(kind_added)
        opened += int(kind_opened)
    return LibraryOverviewResponse(
        total=total,
        by_kind=LibraryKindCounts(**by_kind),
        unopened=unopened,
        starred=starred,
        added_7d=added,
        opened_7d=opened,
    )
