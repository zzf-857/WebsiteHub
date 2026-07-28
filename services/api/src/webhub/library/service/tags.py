from __future__ import annotations

from typing import Literal

from sqlalchemy import and_, exists, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.db.locking import reserve_account_taxonomy
from webhub.db.models import (
    Site,
    SiteMetadataPreference,
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
    if not await reserve_account_taxonomy(session, user_id):
        raise LibraryConflictError("账号状态已发生变化，请刷新后重试")
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
    display, normalized = _display_name(name, maximum=40, field="标签名称")
    if not await reserve_account_taxonomy(session, user_id):
        raise LibraryConflictError("账号状态已发生变化，请刷新后重试")
    tag = await _owned_tag(session, user_id, tag_id)
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
    if not await reserve_account_taxonomy(session, user_id):
        raise LibraryConflictError("账号状态已发生变化，请刷新后重试")
    tag = await _owned_tag(session, user_id, tag_id)
    site_filter = exists(
        select(SiteTag.site_id).where(
            SiteTag.user_id == user_id,
            SiteTag.site_id == Site.id,
            SiteTag.tag_id == tag.id,
        )
    )
    linked_site_ids = list(
        (
            await session.scalars(
                select(SiteTag.site_id).where(
                    SiteTag.user_id == user_id,
                    SiteTag.tag_id == tag.id,
                )
            )
        ).all()
    )
    linked = len(linked_site_ids)
    if linked_site_ids:
        existing_preference_ids = set(
            (
                await session.scalars(
                    select(SiteMetadataPreference.site_id)
                    .join(
                        SiteTag,
                        and_(
                            SiteTag.user_id == SiteMetadataPreference.user_id,
                            SiteTag.site_id == SiteMetadataPreference.site_id,
                        ),
                    )
                    .where(
                        SiteMetadataPreference.user_id == user_id,
                        SiteTag.tag_id == tag.id,
                    )
                )
            ).all()
        )
        linked_preference = exists(
            select(SiteTag.site_id).where(
                SiteTag.user_id == user_id,
                SiteTag.site_id == SiteMetadataPreference.site_id,
                SiteTag.tag_id == tag.id,
            )
        )
        await session.execute(
            update(SiteMetadataPreference)
            .where(
                SiteMetadataPreference.user_id == user_id,
                linked_preference,
            )
            .values(tags_are_manual=True, tags_are_llm=False, updated_at=utc_now())
        )
        session.add_all(
            SiteMetadataPreference(
                user_id=user_id,
                site_id=site_id,
                tags_are_manual=True,
            )
            for site_id in linked_site_ids
            if site_id not in existing_preference_ids
        )
    await session.execute(
        update(Site)
        .where(Site.user_id == user_id, site_filter)
        .values(version=Site.version + 1, updated_at=utc_now())
    )
    await session.delete(tag)
    await session.commit()
    return TagDeleteResponse(message="标签已删除，网站保留不变", unlinked_site_count=linked)
