from __future__ import annotations

from typing import Literal

from sqlalchemy import and_, exists, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.db.models import (
    Site,
    SiteTag,
    Tag,
    utc_now,
)
from webhub.library.schemas import (
    TagDeleteResponse,
    TagListResponse,
    TagResponse,
)

from ._common import (
    LibraryConflictError,
    _display_name,
    _owned_tag,
    _tag_response,
)

SortKey = Literal["created", "updated", "name", "custom"]
SortDirection = Literal["asc", "desc"]


async def list_tags(session: AsyncSession, user_id: str) -> TagListResponse:
    rows = (
        await session.execute(
            select(Tag, func.count(SiteTag.site_id))
            .outerjoin(
                SiteTag,
                and_(SiteTag.user_id == Tag.user_id, SiteTag.tag_id == Tag.id),
            )
            .where(Tag.user_id == user_id)
            .group_by(Tag.id)
            .order_by(Tag.normalized_name, Tag.id)
        )
    ).all()
    return TagListResponse(items=[_tag_response(tag, int(count)) for tag, count in rows])


async def create_tag(session: AsyncSession, user_id: str, name: str) -> TagResponse:
    display, normalized = _display_name(name, maximum=40, field="标签名称")
    tag = Tag(user_id=user_id, name=display, normalized_name=normalized)
    session.add(tag)
    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise LibraryConflictError("标签名称已存在") from error
    return _tag_response(tag, 0)


async def update_tag(
    session: AsyncSession,
    user_id: str,
    tag_id: str,
    name: str,
) -> TagResponse:
    tag = await _owned_tag(session, user_id, tag_id)
    display, normalized = _display_name(name, maximum=40, field="标签名称")
    tag.name = display
    tag.normalized_name = normalized
    tag.updated_at = utc_now()
    site_filter = exists(
        select(SiteTag.site_id).where(
            SiteTag.user_id == user_id,
            SiteTag.site_id == Site.id,
            SiteTag.tag_id == tag.id,
        )
    )
    try:
        await session.execute(
            update(Site)
            .where(Site.user_id == user_id, site_filter)
            .values(version=Site.version + 1, updated_at=utc_now())
        )
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise LibraryConflictError("标签名称已存在") from error
    count = int(
        await session.scalar(
            select(func.count(SiteTag.site_id)).where(
                SiteTag.user_id == user_id,
                SiteTag.tag_id == tag.id,
            )
        )
        or 0
    )
    return _tag_response(tag, count)


async def delete_tag(
    session: AsyncSession,
    user_id: str,
    tag_id: str,
) -> TagDeleteResponse:
    tag = await _owned_tag(session, user_id, tag_id)
    site_filter = exists(
        select(SiteTag.site_id).where(
            SiteTag.user_id == user_id,
            SiteTag.site_id == Site.id,
            SiteTag.tag_id == tag.id,
        )
    )
    linked = int(
        await session.scalar(
            select(func.count(SiteTag.site_id)).where(
                SiteTag.user_id == user_id,
                SiteTag.tag_id == tag.id,
            )
        )
        or 0
    )
    await session.execute(
        update(Site)
        .where(Site.user_id == user_id, site_filter)
        .values(version=Site.version + 1, updated_at=utc_now())
    )
    await session.delete(tag)
    await session.commit()
    return TagDeleteResponse(message="标签已删除，网站保留不变", unlinked_site_count=linked)
