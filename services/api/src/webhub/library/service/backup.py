"""Library-owned backup writes. Caller owns locking and the enclosing transaction."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.db.models import Category, Site, SiteMetadataPreference, SiteTag, Tag
from webhub.library.icons import CATEGORY_ICON_NAMES, infer_category_icon
from webhub.library.kinds import KIND_SOURCES, SITE_KINDS, infer_kind
from webhub.library.service._common import (
    _display_name,
    _owned_category,
    _owned_tags,
    _site_meta,
    _site_url,
)


def normalize_backup_name(value: str, *, maximum: int) -> tuple[str, str]:
    return _display_name(value, maximum=maximum, field="名称")


async def restore_category(
    session: AsyncSession, user_id: str, *, name: str, icon: str
) -> tuple[str, bool]:
    display, normalized = normalize_backup_name(name, maximum=80)
    row = await session.scalar(
        select(Category).where(Category.user_id == user_id, Category.normalized_name == normalized)
    )
    if row is not None:
        return row.id, False
    row = Category(
        user_id=user_id,
        name=display,
        normalized_name=normalized,
        is_default=False,
        icon=icon if icon in CATEGORY_ICON_NAMES else infer_category_icon(display),
    )
    session.add(row)
    await session.flush()
    return row.id, True


async def restore_tag(session: AsyncSession, user_id: str, *, name: str) -> tuple[str, bool]:
    display, normalized = normalize_backup_name(name, maximum=40)
    row = await session.scalar(
        select(Tag).where(Tag.user_id == user_id, Tag.normalized_name == normalized)
    )
    if row is not None:
        return row.id, False
    row = Tag(user_id=user_id, name=display, normalized_name=normalized)
    session.add(row)
    await session.flush()
    return row.id, True


@dataclass(frozen=True, slots=True)
class SiteBackup:
    url: str
    name: str
    summary: str
    description: str
    kind: str
    kind_source: str
    pinned: bool
    favicon_url: str | None
    preview_url: str | None
    meta: dict[str, Any]
    created_at: datetime
    open_count: int
    last_opened_at: datetime | None
    seen_at: datetime | None
    starred_at: datetime | None
    resurface_muted: bool


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _time_merge(left: datetime | None, right: datetime | None, *, latest: bool) -> datetime | None:
    values = [_utc(value) for value in (left, right) if value is not None]
    return (max(values) if latest else min(values)) if values else None


def _merge_meta(current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    result = current.copy()
    for key, value in incoming.items():
        if key not in result:
            result[key] = value
        elif isinstance(value, dict) and isinstance(result[key], dict):
            result[key] = _merge_meta(result[key], value)
    return result


async def restore_site(
    session: AsyncSession, user_id: str, data: SiteBackup, *, category_id: str, tag_ids: list[str]
) -> tuple[str, bool]:
    name, normalized_name = normalize_backup_name(data.name, maximum=160)
    original_url, identity_url = _site_url(data.url)
    category = await _owned_category(session, user_id, category_id)
    tags = await _owned_tags(session, user_id, list(dict.fromkeys(tag_ids)))
    row = await session.scalar(
        select(Site).where(Site.user_id == user_id, Site.identity_url == identity_url)
    )
    created = row is None
    if row is None:
        position = int(
            await session.scalar(
                select(func.coalesce(func.max(Site.position), -1) + 1).where(
                    Site.user_id == user_id, Site.category_id == category_id
                )
            )
            or 0
        )
        row = Site(
            user_id=user_id,
            category_id=category_id,
            position=position,
            name=name,
            normalized_name=normalized_name,
            original_url=original_url,
            identity_url=identity_url,
            source="backup",
            summary=data.summary,
            description=data.description,
            favicon_url=data.favicon_url,
            preview_url=data.preview_url,
            pinned=data.pinned,
            kind=data.kind if data.kind in SITE_KINDS else infer_kind(identity_url),
            kind_source=data.kind_source if data.kind_source in KIND_SOURCES else "auto",
            meta_json=json.dumps(data.meta, ensure_ascii=False),
            created_at=_utc(data.created_at),
            analysis_status="complete" if data.summary or data.description else "not_analyzed",
            open_count=data.open_count,
            last_opened_at=_utc(data.last_opened_at) if data.last_opened_at is not None else None,
            seen_at=_utc(data.seen_at) if data.seen_at is not None else None,
            starred_at=_utc(data.starred_at) if data.starred_at is not None else None,
            resurface_muted=data.resurface_muted,
        )
        session.add(row)
        await session.flush()
        # Restored choices are user-owned, just as explicitly provided create-form values are.
        session.add(
            SiteMetadataPreference(
                user_id=user_id,
                site_id=row.id,
                summary_is_manual=bool(data.summary),
                description_is_manual=bool(data.description),
                favicon_is_manual=bool(data.favicon_url),
                category_is_manual=not category.is_default,
                tags_are_manual=bool(tags),
            )
        )
    else:
        await session.execute(
            update(Site)
            .where(Site.user_id == user_id, Site.id == row.id)
            .values(
                summary=row.summary or data.summary,
                description=row.description or data.description,
                favicon_url=row.favicon_url or data.favicon_url,
                preview_url=row.preview_url or data.preview_url,
                meta_json=json.dumps(
                    _merge_meta(_site_meta(row.meta_json), data.meta), ensure_ascii=False
                ),
                open_count=max(row.open_count, data.open_count),
                last_opened_at=_time_merge(row.last_opened_at, data.last_opened_at, latest=True),
                seen_at=_time_merge(row.seen_at, data.seen_at, latest=False),
                starred_at=_time_merge(row.starred_at, data.starred_at, latest=False),
                updated_at=Site.updated_at,
            )
        )
    linked = set(
        (
            await session.scalars(
                select(SiteTag.tag_id).where(SiteTag.user_id == user_id, SiteTag.site_id == row.id)
            )
        ).all()
    )
    session.add_all(
        SiteTag(user_id=user_id, site_id=row.id, tag_id=tag.id)
        for tag in tags
        if tag.id not in linked
    )
    await session.flush()
    return row.id, created
