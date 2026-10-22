"""Content snapshots (Q48): the latest page text or README excerpt per site.

The analysis flow already reads a bounded amount of visible page text
(``MAX_PAGE_TEXT_CHARS``) and, for GitHub repositories, a README excerpt. This
module keeps the latest of them so the library and MCP search can match on
what a saved page actually says. Nothing here fetches anything: private or
sensitive URLs that the fetcher refuses simply never produce a snapshot, and
existing sites get one only on their next analysis.
"""

from __future__ import annotations

import unicodedata
from typing import Literal

from sqlalchemy import literal, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.db.models import Site, SiteContent, utc_now

from .metadata import MAX_PAGE_TEXT_CHARS

ContentSource = Literal["page", "readme"]
MAX_CONTENT_CHARS = MAX_PAGE_TEXT_CHARS


def clean_content(text: str) -> str:
    """Collapse whitespace per line and drop empty lines; bound the length."""

    normalized = unicodedata.normalize("NFC", text or "")
    lines = (" ".join(line.split()) for line in normalized.splitlines())
    return "\n".join(line for line in lines if line)[:MAX_CONTENT_CHARS].strip()


def choose_snapshot(page_text: str, readme_excerpt: str) -> tuple[str, ContentSource] | None:
    """README wins for repositories: it is the project's own words, while the
    hosting page mixes in navigation. Otherwise the visible page text."""

    readme = clean_content(readme_excerpt)
    if readme:
        return readme, "readme"
    page = clean_content(page_text)
    if page:
        return page, "page"
    return None


async def store_content_snapshot(
    session: AsyncSession,
    *,
    user_id: str,
    site_id: str,
    url: str,
    content: str,
    source: ContentSource,
) -> bool:
    """Upsert the snapshot while the site still points at ``url``; commit.

    The text describes the page behind that URL, so a URL change during the
    fetch discards it (same fence as the GitHub stats). It never changes the
    site's ``version`` or ``updated_at``.
    """

    content = clean_content(content)
    if not content:
        return False
    now = utc_now()
    source_rows = select(
        Site.id,
        Site.user_id,
        literal(content),
        literal(len(content)),
        literal(source),
        literal(now),
    ).where(Site.user_id == user_id, Site.id == site_id, Site.original_url == url)
    statement = insert(SiteContent).from_select(
        ["site_id", "user_id", "content", "char_count", "source", "extracted_at"],
        source_rows,
    )
    statement = statement.on_conflict_do_update(
        index_elements=[SiteContent.site_id],
        set_={
            "content": statement.excluded.content,
            "char_count": statement.excluded.char_count,
            "source": statement.excluded.source,
            "extracted_at": statement.excluded.extracted_at,
        },
        where=SiteContent.user_id == statement.excluded.user_id,
    )
    result = await session.execute(statement)
    await session.commit()
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def load_content(
    session: AsyncSession, user_id: str, site_id: str
) -> SiteContent | None:
    return await session.scalar(
        select(SiteContent).where(
            SiteContent.user_id == user_id, SiteContent.site_id == site_id
        )
    )


__all__ = [
    "MAX_CONTENT_CHARS",
    "ContentSource",
    "choose_snapshot",
    "clean_content",
    "load_content",
    "store_content_snapshot",
]
