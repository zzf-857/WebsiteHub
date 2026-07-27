"""Apply a fetch outcome to one stored Site.

The rule that shapes everything here: **analysis fills blanks, it never
overwrites the user.**  A name or description the user typed is a decision;
a page's ``<title>`` is a guess about that decision.  Guesses lose.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.db.database import Database
from webhub.db.models import Site, utc_now

from .fetcher import DEFAULT_TIMEOUT_SECONDS, FetchOutcome, fetch_site_metadata

_LOGGER = logging.getLogger(__name__)

MAX_DESCRIPTION_CHARS = 1_000


async def _owned_site(session: AsyncSession, user_id: str, site_id: str) -> Site | None:
    return await session.scalar(select(Site).where(Site.user_id == user_id, Site.id == site_id))


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
    user_id: str,
    site_id: str,
    outcome: FetchOutcome,
) -> Site | None:
    """Write one analysis result onto the site, filling only empty fields."""

    site = await _owned_site(session, user_id, site_id)
    if site is None:
        return None

    metadata = outcome.metadata
    # Description: only when the user left it empty.  Overwriting a sentence
    # someone wrote with a marketing meta tag is a downgrade, not an upgrade.
    if not (site.description or "").strip() and metadata.description:
        site.description = metadata.description[:MAX_DESCRIPTION_CHARS]
    if not (site.favicon_url or "").strip():
        icon = _safe_image_url(metadata.icon_url)
        if icon:
            site.favicon_url = icon
    # og:image / twitter:image 已经被 metadata.py 解析并转成绝对地址了，
    # 落到 preview_url 才算走完这条链路；同样只补空、不覆盖。
    if not (site.preview_url or "").strip():
        preview = _safe_image_url(metadata.image_url)
        if preview:
            site.preview_url = preview

    site.analysis_status = outcome.status
    # **不 bump version。** version 是给用户可见的并发编辑用的乐观锁；
    # 后台分析是系统侧的补白，涨版本号等于系统跟用户抢锁——用户刚建完网站
    # 马上编辑就会撞上「已被修改，请刷新后重试」，而"改动"是我们自己做的。
    # 代价是分析与用户编辑并发时用户那次会覆盖掉补白，这与本模块「用户永远赢」
    # 的规则一致。
    site.updated_at = utc_now()
    await session.commit()
    return site


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

    site = await _owned_site(session, user_id, site_id)
    if site is None:
        return None

    url = site.original_url
    # `pending` is committed before the network call so a page open during a
    # slow fetch shows "分析中" instead of a stale "未分析".
    site.analysis_status = "pending"
    site.updated_at = utc_now()
    await session.commit()

    outcome = await fetch_site_metadata(url, timeout_seconds=timeout_seconds)
    await apply_outcome(session, user_id, site_id, outcome)
    return outcome


async def analyze_in_background(
    database: Database,
    user_id: str,
    site_id: str,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> None:
    """Run one analysis detached from the request that triggered it.

    Saving a site must not wait on someone else's slow server, and must not
    fail because that server is down.
    """

    try:
        async with database.sessions() as session:
            await analyze_site(session, user_id, site_id, timeout_seconds=timeout_seconds)
    except Exception as error:  # noqa: BLE001 - a background failure must stay contained
        _LOGGER.warning("site analysis failed for %s", site_id, exc_info=error)
        try:
            async with database.sessions() as session:
                site = await _owned_site(session, user_id, site_id)
                if site is not None and site.analysis_status == "pending":
                    # Never leave a row stuck in `pending`; that reads as
                    # "still working" forever.
                    site.analysis_status = "failed"
                    site.updated_at = utc_now()
                    await session.commit()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("could not record analysis failure for %s", site_id)


__all__ = ["analyze_in_background", "analyze_site", "apply_outcome"]
