"""Feed-owned recovery: no network and no independent transaction."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.db.models import Feed
from webhub.feeds.service import normalize_feed_url


async def restore_feed(
    session: AsyncSession,
    user_id: str,
    *,
    url: str,
    title: str,
    kind: str,
    enabled: bool,
    now: datetime,
) -> tuple[str, bool]:
    normalized = normalize_feed_url(url)
    row = await session.scalar(select(Feed).where(Feed.user_id == user_id, Feed.url == normalized))
    if row is not None:
        return row.id, False
    row = Feed(
        user_id=user_id, url=normalized, title=title, kind=kind, enabled=enabled, next_fetch_at=now
    )
    session.add(row)
    await session.flush()
    return row.id, True
