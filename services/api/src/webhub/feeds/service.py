"""Subscriptions, polling, retention, daily picks and "收进收藏库".

Every query is scoped by ``user_id``; a feed or item id from another account
reads as "not found".  Polling state lives in the database (``next_fetch_at``
and ``consecutive_failures``), so a restart simply resumes the schedule.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.db.database import Database
from webhub.db.models import (
    DEFAULT_CATEGORY_NAME,
    Category,
    Feed,
    FeedItem,
    Site,
    SiteTag,
    Tag,
    utc_now,
)
from webhub.library import service as library_service
from webhub.library.schemas import SiteCreateRequest

from . import fetch as feed_fetch
from .fetch import FeedFetcher
from .parser import FeedParseError, ParsedFeed, parse_feed

POLL_INTERVAL = timedelta(minutes=60)
MAX_BACKOFF = timedelta(hours=24)
RETENTION = timedelta(days=30)
MAX_FEEDS_PER_ACCOUNT = 100
MAX_ITEMS_PER_FEED = 200
MAX_FEED_URL_LENGTH = 2_048
DIGEST_WINDOW = timedelta(days=7)
DIGEST_LIMIT = 10
DIGEST_PER_SOURCE = 3
MAX_KEYWORDS = 40
_DECAY_HALF_LIFE_HOURS = 36.0
_LOGGER = logging.getLogger(__name__)
# Feeds being refreshed right now in this process: a manual refresh racing the
# poller would otherwise insert the same guids twice and trip the unique index.
_IN_FLIGHT: set[str] = set()
_ASCII_WORD = re.compile(r"^[0-9a-z][0-9a-z .+#-]*$")


class FeedError(Exception):
    status_code = 400
    code = "feed_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class FeedNotFoundError(FeedError):
    status_code = 404
    code = "not_found"


class FeedConflictError(FeedError):
    status_code = 409
    code = "conflict"


class FeedValidationError(FeedError):
    status_code = 422
    code = "validation_error"


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def backoff_delay(failures: int) -> timedelta:
    """Poll interval after ``failures`` consecutive failures: 2h, 4h, 8h … ≤24h."""

    if failures <= 0:
        return POLL_INTERVAL
    return min(POLL_INTERVAL * (2 ** min(failures, 10)), MAX_BACKOFF)


def normalize_feed_url(value: str) -> str:
    url = value.strip()
    if not url or len(url) > MAX_FEED_URL_LENGTH:
        raise FeedValidationError("订阅地址无效", code="invalid_url")
    try:
        parsed = urlsplit(url)
    except ValueError as error:
        raise FeedValidationError("订阅地址无效", code="invalid_url") from error
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise FeedValidationError("订阅地址必须是 http(s) 链接", code="invalid_url")
    if parsed.username or parsed.password:
        raise FeedValidationError("订阅地址不能包含账号密码", code="invalid_url")
    if any(ord(character) < 0x21 for character in url):
        raise FeedValidationError("订阅地址无效", code="invalid_url")
    return url


# ----------------------------------------------------------------- feeds CRUD


async def owned_feed(session: AsyncSession, user_id: str, feed_id: str) -> Feed:
    feed = await session.scalar(select(Feed).where(Feed.user_id == user_id, Feed.id == feed_id))
    if feed is None:
        raise FeedNotFoundError("订阅源不存在")
    return feed


async def owned_item(session: AsyncSession, user_id: str, item_id: str) -> FeedItem:
    item = await session.scalar(
        select(FeedItem).where(FeedItem.user_id == user_id, FeedItem.id == item_id)
    )
    if item is None:
        raise FeedNotFoundError("条目不存在")
    return item


async def list_feeds(session: AsyncSession, user_id: str) -> list[tuple[Feed, int]]:
    counts = dict(
        (
            await session.execute(
                select(FeedItem.feed_id, func.count())
                .where(FeedItem.user_id == user_id)
                .group_by(FeedItem.feed_id)
            )
        ).all()
    )
    feeds = (
        await session.scalars(
            select(Feed).where(Feed.user_id == user_id).order_by(Feed.created_at, Feed.id)
        )
    ).all()
    return [(feed, int(counts.get(feed.id, 0))) for feed in feeds]


async def create_feed(session: AsyncSession, user_id: str, url: str) -> Feed:
    normalized = normalize_feed_url(url)
    existing = await session.scalar(
        select(Feed.id).where(Feed.user_id == user_id, Feed.url == normalized)
    )
    if existing is not None:
        raise FeedConflictError("已经订阅过这个地址", code="duplicate_feed")
    total = await session.scalar(select(func.count()).where(Feed.user_id == user_id))
    if int(total or 0) >= MAX_FEEDS_PER_ACCOUNT:
        raise FeedConflictError(
            f"最多订阅 {MAX_FEEDS_PER_ACCOUNT} 个来源", code="feed_limit_reached"
        )
    feed = Feed(
        user_id=user_id,
        url=normalized,
        title=urlsplit(normalized).hostname or normalized[:200],
        kind="rss",
        next_fetch_at=utc_now(),
    )
    session.add(feed)
    await session.commit()
    return feed


async def set_feed_enabled(
    session: AsyncSession, user_id: str, feed_id: str, enabled: bool
) -> Feed:
    feed = await owned_feed(session, user_id, feed_id)
    feed.enabled = enabled
    if enabled:
        # Re-enabling is an explicit "try again": clear the backoff.
        feed.consecutive_failures = 0
        feed.next_fetch_at = utc_now()
    await session.commit()
    return feed


async def delete_feed(session: AsyncSession, user_id: str, feed_id: str) -> None:
    feed = await owned_feed(session, user_id, feed_id)
    # Items cascade; resources already saved from them stay in ``sites``.
    await session.delete(feed)
    await session.commit()


# ---------------------------------------------------------------- polling


@dataclass(frozen=True, slots=True)
class RefreshOutcome:
    status: str
    inserted: int = 0
    error: str | None = None


async def _parse(body: bytes) -> ParsedFeed:
    return await asyncio.to_thread(parse_feed, body)


async def refresh_feed(
    database: Database,
    feed_id: str,
    *,
    fetcher: FeedFetcher | None = None,
    now: datetime | None = None,
) -> RefreshOutcome:
    """Fetch one feed (conditional GET), store new items, update the schedule.

    The network call happens outside any database transaction.
    """

    if feed_id in _IN_FLIGHT:
        return RefreshOutcome("busy")
    _IN_FLIGHT.add(feed_id)
    try:
        return await _refresh_feed(database, feed_id, fetcher=fetcher, now=now)
    finally:
        _IN_FLIGHT.discard(feed_id)


async def _refresh_feed(
    database: Database,
    feed_id: str,
    *,
    fetcher: FeedFetcher | None,
    now: datetime | None,
) -> RefreshOutcome:
    async with database.sessions() as session:
        feed = await session.get(Feed, feed_id)
        if feed is None:
            return RefreshOutcome("missing")
        url, etag, last_modified = feed.url, feed.etag, feed.last_modified

    selected_fetcher = fetcher or feed_fetch.fetch_feed
    result = await selected_fetcher(url, etag, last_modified)
    parsed: ParsedFeed | None = None
    error = result.error if result.status == "error" else None
    if result.status == "ok":
        try:
            parsed = await _parse(result.body)
        except FeedParseError:
            error = "not_a_feed"
        except Exception:  # noqa: BLE001 - a hostile document must not kill polling
            error = "not_a_feed"

    moment = now or utc_now()
    async with database.sessions() as session:
        feed = await session.get(Feed, feed_id)
        if feed is None:
            return RefreshOutcome("missing")
        feed.last_fetched_at = moment
        if error is not None:
            feed.consecutive_failures = int(feed.consecutive_failures or 0) + 1
            feed.last_error = error[:64]
            feed.next_fetch_at = moment + backoff_delay(feed.consecutive_failures)
            await session.commit()
            return RefreshOutcome("error", error=error)

        feed.consecutive_failures = 0
        feed.last_error = None
        feed.next_fetch_at = moment + POLL_INTERVAL
        inserted = 0
        if parsed is not None:
            feed.etag = result.etag
            feed.last_modified = result.last_modified
            if parsed.title:
                feed.title = parsed.title
            feed.site_url = parsed.site_url
            feed.kind = parsed.kind
            inserted = await _store_items(session, feed, parsed, moment)
        await session.commit()
        return RefreshOutcome("ok" if parsed is not None else "not_modified", inserted=inserted)


async def _store_items(
    session: AsyncSession, feed: Feed, parsed: ParsedFeed, moment: datetime
) -> int:
    guids = [item.guid for item in parsed.items]
    known = set(
        (
            await session.scalars(
                select(FeedItem.guid).where(FeedItem.feed_id == feed.id, FeedItem.guid.in_(guids))
            )
        ).all()
    )
    cutoff = moment - RETENTION
    inserted = 0
    for item in parsed.items:
        if item.guid in known:
            continue
        published = item.published_at
        # Older than the retention window: cleanup would delete it again, and
        # it is not news.  Future dates are clamped so they cannot pin the top.
        if published is not None and published < cutoff:
            continue
        if published is not None and published > moment:
            published = moment
        session.add(
            FeedItem(
                feed_id=feed.id,
                user_id=feed.user_id,
                guid=item.guid,
                url=item.url,
                title=item.title,
                summary=item.summary,
                author=item.author,
                published_at=published,
                audio_url=item.audio_url,
                duration_seconds=item.duration_seconds,
                fetched_at=moment,
            )
        )
        known.add(item.guid)
        inserted += 1
    await session.flush()
    await _trim_feed(session, feed.id)
    return inserted


async def _trim_feed(session: AsyncSession, feed_id: str) -> None:
    """Keep at most ``MAX_ITEMS_PER_FEED`` unsaved items per source."""

    keep = (
        select(FeedItem.id)
        .where(FeedItem.feed_id == feed_id, FeedItem.saved_site_id.is_(None))
        .order_by(
            func.coalesce(FeedItem.published_at, FeedItem.fetched_at).desc(), FeedItem.id.desc()
        )
        .limit(MAX_ITEMS_PER_FEED)
    )
    await session.execute(
        delete(FeedItem).where(
            FeedItem.feed_id == feed_id,
            FeedItem.saved_site_id.is_(None),
            FeedItem.id.not_in(keep.scalar_subquery()),
        )
    )


async def cleanup_items(database: Database, *, now: datetime | None = None) -> int:
    """Delete stream items older than 30 days unless they were saved."""

    cutoff = (now or utc_now()) - RETENTION
    async with database.sessions() as session:
        result = await session.execute(
            delete(FeedItem).where(
                FeedItem.saved_site_id.is_(None),
                func.coalesce(FeedItem.published_at, FeedItem.fetched_at) < cutoff,
            )
        )
        await session.commit()
        return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def due_feed_ids(database: Database, *, now: datetime, limit: int = 20) -> list[str]:
    async with database.sessions() as session:
        rows = await session.scalars(
            select(Feed.id)
            .where(
                Feed.enabled.is_(True),
                or_(Feed.next_fetch_at.is_(None), Feed.next_fetch_at <= now),
            )
            .order_by(Feed.next_fetch_at, Feed.id)
            .limit(limit)
        )
        return list(rows.all())


async def poll_due_feeds(
    database: Database,
    *,
    fetcher: FeedFetcher | None = None,
    now: datetime | None = None,
    limit: int = 20,
) -> int:
    moment = now or utc_now()
    feed_ids = await due_feed_ids(database, now=moment, limit=limit)
    for feed_id in feed_ids:
        try:
            await refresh_feed(database, feed_id, fetcher=fetcher, now=moment)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one broken feed must not starve the others
            _LOGGER.exception("refreshing feed %s failed", feed_id)
    return len(feed_ids)


# ------------------------------------------------------------- item actions


async def list_items(
    session: AsyncSession,
    user_id: str,
    *,
    feed_id: str | None = None,
    limit: int = 200,
) -> list[FeedItem]:
    conditions = [FeedItem.user_id == user_id, FeedItem.dismissed_at.is_(None)]
    if feed_id is not None:
        conditions.append(FeedItem.feed_id == feed_id)
    rows = await session.scalars(
        select(FeedItem)
        .where(*conditions)
        .order_by(
            func.coalesce(FeedItem.published_at, FeedItem.fetched_at).desc(), FeedItem.id.desc()
        )
        .limit(limit)
    )
    return list(rows.all())


async def update_item_state(
    session: AsyncSession,
    user_id: str,
    item_id: str,
    *,
    read: bool | None = None,
    dismissed: bool | None = None,
) -> FeedItem:
    item = await owned_item(session, user_id, item_id)
    moment = utc_now()
    if read is not None:
        item.read_at = (item.read_at or moment) if read else None
    if dismissed is not None:
        item.dismissed_at = (item.dismissed_at or moment) if dismissed else None
    await session.commit()
    return item


async def save_item_to_library(
    session: AsyncSession, user_id: str, item_id: str
) -> tuple[FeedItem, str, bool]:
    """Create (or link) the library resource for one item.

    Returns ``(item, site_id, created)``; ``created`` is False when the URL was
    already in the library and the item is merely linked to it.
    """

    item = await owned_item(session, user_id, item_id)
    if item.saved_site_id is not None:
        exists = await session.scalar(
            select(Site.id).where(Site.user_id == user_id, Site.id == item.saved_site_id)
        )
        if exists is not None:
            return item, item.saved_site_id, False
    if not item.url:
        raise FeedValidationError("这个条目没有可收藏的链接", code="item_without_url")
    try:
        _, identity_url = library_service._site_url(item.url)
    except library_service.LibraryValidationError as error:
        raise FeedValidationError(error.message, code="invalid_url") from error

    async def existing_site_id() -> str | None:
        return await session.scalar(
            select(Site.id).where(Site.user_id == user_id, Site.identity_url == identity_url)
        )

    site_id = await existing_site_id()
    created = False
    if site_id is None:
        payload = SiteCreateRequest(
            name=(item.title or identity_url)[:160],
            url=item.url,
            description=item.summary[:4_000],
            source="manual",
        )
        try:
            site = await library_service.create_site(session, user_id, payload)
            site_id, created = site.id, True
        except library_service.LibraryConflictError as error:
            site_id = await existing_site_id() if error.code == "duplicate_url" else None
            if site_id is None:
                raise FeedConflictError(error.message, code=error.code) from error
        except library_service.LibraryError as error:
            raise FeedValidationError(error.message, code=error.code) from error
    item = await owned_item(session, user_id, item_id)
    item.saved_site_id = site_id
    item.read_at = item.read_at or utc_now()
    await session.commit()
    return item, site_id, created


# ------------------------------------------------------------------ digest


@dataclass(frozen=True, slots=True)
class Keyword:
    text: str
    weight: float
    ascii_word: bool


@dataclass(frozen=True, slots=True)
class Pick:
    item: FeedItem
    feed_title: str
    feed_kind: str
    score: float
    matched: list[str]
    reason: str


async def library_keywords(session: AsyncSession, user_id: str) -> list[Keyword]:
    """The account's most used tags and categories, weighted by usage."""

    tag_rows = (
        await session.execute(
            select(Tag.name, func.count(SiteTag.site_id).label("uses"))
            .join(SiteTag, and_(SiteTag.user_id == Tag.user_id, SiteTag.tag_id == Tag.id))
            .where(Tag.user_id == user_id)
            .group_by(Tag.id, Tag.name)
            .order_by(func.count(SiteTag.site_id).desc(), Tag.name)
            .limit(MAX_KEYWORDS)
        )
    ).all()
    category_rows = (
        await session.execute(
            select(Category.name, func.count(Site.id))
            .join(Site, and_(Site.user_id == Category.user_id, Site.category_id == Category.id))
            .where(
                Category.user_id == user_id,
                Category.is_default.is_(False),
                Category.name != DEFAULT_CATEGORY_NAME,
            )
            .group_by(Category.id, Category.name)
            .order_by(func.count(Site.id).desc(), Category.name)
            .limit(MAX_KEYWORDS // 2)
        )
    ).all()
    merged: dict[str, Keyword] = {}
    for name, uses in [*tag_rows, *category_rows]:
        text = " ".join(str(name).split()).casefold()
        if len(text) < 2:
            continue
        weight = math.log1p(int(uses))
        current = merged.get(text)
        if current is None or current.weight < weight:
            merged[text] = Keyword(text, weight, bool(_ASCII_WORD.match(text)))
    return sorted(merged.values(), key=lambda keyword: (-keyword.weight, keyword.text))


def _contains(haystack: str, keyword: Keyword) -> bool:
    if not keyword.ascii_word:
        return keyword.text in haystack
    # "ai" must not match "said": ASCII keywords need word boundaries.
    pattern = r"(?<![0-9a-z])" + re.escape(keyword.text) + r"(?![0-9a-z])"
    return re.search(pattern, haystack) is not None


def score_item(
    *,
    title: str,
    summary: str,
    published: datetime,
    now: datetime,
    keywords: Sequence[Keyword],
) -> tuple[float, list[str]]:
    """Affinity with the library (0.65) plus recency decay (0.35)."""

    title_text = title.casefold()
    summary_text = summary.casefold()
    raw = 0.0
    matched: list[str] = []
    for keyword in keywords:
        if _contains(title_text, keyword):
            raw += keyword.weight
            matched.append(keyword.text)
        elif _contains(summary_text, keyword):
            raw += keyword.weight * 0.5
            matched.append(keyword.text)
    reference = keywords[0].weight if keywords else 1.0
    affinity = raw / (raw + reference) if raw > 0 else 0.0
    age_hours = max(0.0, (now - published).total_seconds() / 3600)
    decay = 0.5 ** (age_hours / _DECAY_HALF_LIFE_HOURS)
    return 0.65 * affinity + 0.35 * decay, matched


def pick_reason(matched: Sequence[str]) -> str:
    if matched:
        return "与你常收藏的" + "".join(f"「{name}」" for name in matched[:3]) + "相关"
    return "订阅源近期更新"


async def daily_picks(
    session: AsyncSession,
    user_id: str,
    *,
    limit: int = DIGEST_LIMIT,
    per_source: int = DIGEST_PER_SOURCE,
    now: datetime | None = None,
) -> list[Pick]:
    moment = _utc(now) or utc_now()
    window_start = moment - DIGEST_WINDOW
    rows = (
        await session.execute(
            select(FeedItem, Feed.title, Feed.kind)
            .join(Feed, and_(Feed.user_id == FeedItem.user_id, Feed.id == FeedItem.feed_id))
            .where(
                FeedItem.user_id == user_id,
                FeedItem.dismissed_at.is_(None),
                Feed.enabled.is_(True),
                func.coalesce(FeedItem.published_at, FeedItem.fetched_at) >= window_start,
            )
        )
    ).all()
    keywords = await library_keywords(session, user_id)
    scored: list[Pick] = []
    for item, feed_title, feed_kind in rows:
        published = _utc(item.published_at) or _utc(item.fetched_at) or moment
        score, matched = score_item(
            title=item.title,
            summary=item.summary,
            published=published,
            now=moment,
            keywords=keywords,
        )
        scored.append(
            Pick(item, str(feed_title), str(feed_kind), score, matched, pick_reason(matched))
        )
    scored.sort(key=lambda pick: (-pick.score, pick.item.id))
    picks: list[Pick] = []
    per_feed: dict[str, int] = {}
    for pick in scored:
        used = per_feed.get(pick.item.feed_id, 0)
        if used >= per_source:
            continue
        per_feed[pick.item.feed_id] = used + 1
        picks.append(pick)
        if len(picks) >= limit:
            break
    return picks


__all__ = [
    "FeedConflictError",
    "FeedError",
    "FeedNotFoundError",
    "FeedValidationError",
    "Pick",
    "backoff_delay",
    "cleanup_items",
    "create_feed",
    "daily_picks",
    "delete_feed",
    "list_feeds",
    "list_items",
    "poll_due_feeds",
    "refresh_feed",
    "save_item_to_library",
    "score_item",
    "set_feed_enabled",
    "update_item_state",
]
