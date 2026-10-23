"""Read-only view of where a saved site came from in imported bookmark files.

Answers "来自书签：<目录路径>" on the site detail page from the source ledger
(``site_import_origins`` → ``bookmark_source_occurrences`` →
``bookmark_source_folders``). Every query is scoped to the caller's account; a
site id from another account reads exactly like a missing one.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.db.models import (
    BookmarkSourceFolder,
    BookmarkSourceOccurrence,
    Site,
    SiteImportOrigin,
)
from webhub.library.service import LibraryNotFoundError

# Distinct folder paths returned per site; the UI shows a compact line.
MAX_ORIGIN_PATHS = 20


class SiteImportOriginsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    site_id: str
    occurrence_count: int
    # Each path is the folder chain from the file's root; [] = file root.
    folder_paths: list[list[str]]
    truncated: bool


def _path(display_path: str | None) -> list[str]:
    if not display_path:
        return []
    try:
        parsed = json.loads(display_path)
    except ValueError:
        return []
    if not isinstance(parsed, list):
        return []
    return [entry for entry in parsed if isinstance(entry, str)]


async def site_import_origins(
    session: AsyncSession,
    user_id: str,
    site_id: str,
) -> SiteImportOriginsResponse:
    exists = await session.scalar(
        select(Site.id).where(Site.user_id == user_id, Site.id == site_id)
    )
    if exists is None:
        raise LibraryNotFoundError("网站不存在")

    first_seen = func.min(SiteImportOrigin.created_at)
    first_sequence = func.min(BookmarkSourceOccurrence.source_sequence)
    rows = (
        await session.execute(
            select(BookmarkSourceFolder.display_path, func.count())
            .select_from(SiteImportOrigin)
            .join(
                BookmarkSourceOccurrence,
                and_(
                    BookmarkSourceOccurrence.user_id == SiteImportOrigin.user_id,
                    BookmarkSourceOccurrence.id == SiteImportOrigin.source_occurrence_id,
                ),
            )
            .outerjoin(
                BookmarkSourceFolder,
                and_(
                    BookmarkSourceFolder.user_id == BookmarkSourceOccurrence.user_id,
                    BookmarkSourceFolder.snapshot_id == BookmarkSourceOccurrence.snapshot_id,
                    BookmarkSourceFolder.id == BookmarkSourceOccurrence.folder_id,
                ),
            )
            .where(SiteImportOrigin.user_id == user_id, SiteImportOrigin.site_id == site_id)
            .group_by(BookmarkSourceFolder.display_path)
            .order_by(first_seen, first_sequence)
        )
    ).all()

    paths: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    total = 0
    for display_path, count in rows:
        total += int(count)
        path = _path(display_path)
        key = tuple(path)
        if key in seen:
            continue
        seen.add(key)
        paths.append(path)
    return SiteImportOriginsResponse(
        site_id=site_id,
        occurrence_count=total,
        folder_paths=paths[:MAX_ORIGIN_PATHS],
        truncated=len(paths) > MAX_ORIGIN_PATHS,
    )


__all__ = ["MAX_ORIGIN_PATHS", "SiteImportOriginsResponse", "site_import_origins"]
