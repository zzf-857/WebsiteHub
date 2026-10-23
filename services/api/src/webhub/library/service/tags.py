from __future__ import annotations

from typing import Literal

from sqlalchemy import Exists, and_, delete, exists, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

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
    TagMergePreviewResponse,
    TagMergeResponse,
    TagResponse,
)

from ._common import (
    LibraryConflictError,
    LibraryValidationError,
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


async def _linked_site_ids(session: AsyncSession, user_id: str, tag_id: str) -> list[str]:
    return list(
        (
            await session.scalars(
                select(SiteTag.site_id).where(
                    SiteTag.user_id == user_id,
                    SiteTag.tag_id == tag_id,
                )
            )
        ).all()
    )


async def _mark_tags_manual(
    session: AsyncSession,
    user_id: str,
    tag_id: str,
    linked_site_ids: list[str],
) -> None:
    """Record the user's tag decision on every linked site.

    Deleting or merging a tag is an explicit edit of those sites' tags; a later
    automatic analysis must not quietly add the removed tag back.
    """

    if not linked_site_ids:
        return
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
                    SiteTag.tag_id == tag_id,
                )
            )
        ).all()
    )
    linked_preference = exists(
        select(SiteTag.site_id).where(
            SiteTag.user_id == user_id,
            SiteTag.site_id == SiteMetadataPreference.site_id,
            SiteTag.tag_id == tag_id,
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


def _tagged_with(user_id: str, tag_id: str) -> Exists:
    return exists(
        select(SiteTag.site_id).where(
            SiteTag.user_id == user_id,
            SiteTag.site_id == Site.id,
            SiteTag.tag_id == tag_id,
        )
    )


async def delete_tag(
    session: AsyncSession,
    user_id: str,
    tag_id: str,
) -> TagDeleteResponse:
    if not await reserve_account_taxonomy(session, user_id):
        raise LibraryConflictError("账号状态已发生变化，请刷新后重试")
    tag = await _owned_tag(session, user_id, tag_id)
    linked_site_ids = await _linked_site_ids(session, user_id, tag.id)
    linked = len(linked_site_ids)
    await _mark_tags_manual(session, user_id, tag.id, linked_site_ids)
    await session.execute(
        update(Site)
        .where(Site.user_id == user_id, _tagged_with(user_id, tag.id))
        .values(version=Site.version + 1, updated_at=utc_now())
    )
    await session.delete(tag)
    await session.commit()
    return TagDeleteResponse(message="标签已删除，网站保留不变", unlinked_site_count=linked)


async def _merge_pair(
    session: AsyncSession,
    user_id: str,
    source_id: str,
    target_id: str,
) -> tuple[Tag, Tag]:
    if source_id == target_id:
        raise LibraryValidationError("不能把标签合并到它自己", code="tag_merge_same_tag")
    source = await _owned_tag(session, user_id, source_id)
    target = await _owned_tag(session, user_id, target_id)
    return source, target


async def _merge_counts(
    session: AsyncSession,
    user_id: str,
    source_id: str,
    target_id: str,
) -> tuple[int, int]:
    """(收藏里带源标签的数量, 其中已经带目标标签的数量)。"""

    affected = int(
        await session.scalar(
            select(func.count(SiteTag.site_id)).where(
                SiteTag.user_id == user_id,
                SiteTag.tag_id == source_id,
            )
        )
        or 0
    )
    target_tag = aliased(SiteTag)
    overlap = int(
        await session.scalar(
            select(func.count(SiteTag.site_id))
            .join(
                target_tag,
                and_(
                    target_tag.user_id == SiteTag.user_id,
                    target_tag.site_id == SiteTag.site_id,
                    target_tag.tag_id == target_id,
                ),
            )
            .where(SiteTag.user_id == user_id, SiteTag.tag_id == source_id)
        )
        or 0
    )
    return affected, overlap


async def _tag_count(session: AsyncSession, user_id: str, tag_id: str) -> int:
    return int(
        await session.scalar(
            select(func.count(SiteTag.site_id)).where(
                SiteTag.user_id == user_id,
                SiteTag.tag_id == tag_id,
            )
        )
        or 0
    )


async def tag_merge_preview(
    session: AsyncSession,
    user_id: str,
    source_id: str,
    target_id: str,
) -> TagMergePreviewResponse:
    source, target = await _merge_pair(session, user_id, source_id, target_id)
    affected, overlap = await _merge_counts(session, user_id, source.id, target.id)
    return TagMergePreviewResponse(
        source=_tag_response(source, affected),
        target=_tag_response(target, await _tag_count(session, user_id, target.id)),
        affected_site_count=affected,
        already_tagged_count=overlap,
    )


async def merge_tag(
    session: AsyncSession,
    user_id: str,
    source_id: str,
    target_id: str,
) -> TagMergeResponse:
    """Move every link of ``source`` onto ``target`` and delete ``source``, in one commit."""

    if not await reserve_account_taxonomy(session, user_id):
        raise LibraryConflictError("账号状态已发生变化，请刷新后重试")
    source, target = await _merge_pair(session, user_id, source_id, target_id)
    linked_site_ids = await _linked_site_ids(session, user_id, source.id)
    affected, overlap = await _merge_counts(session, user_id, source.id, target.id)
    if linked_site_ids:
        await _mark_tags_manual(session, user_id, source.id, linked_site_ids)
        already_target = set(await _linked_site_ids(session, user_id, target.id))
        session.add_all(
            SiteTag(user_id=user_id, site_id=site_id, tag_id=target.id)
            for site_id in linked_site_ids
            if site_id not in already_target
        )
        await session.flush()
        await session.execute(
            update(Site)
            .where(Site.user_id == user_id, Site.id.in_(linked_site_ids))
            .values(version=Site.version + 1, updated_at=utc_now())
        )
        await session.execute(
            delete(SiteTag).where(SiteTag.user_id == user_id, SiteTag.tag_id == source.id)
        )
    target.updated_at = utc_now()
    await session.delete(source)
    await session.commit()
    return TagMergeResponse(
        message=f"已把「{source.name}」合并到「{target.name}」",
        target=_tag_response(target, await _tag_count(session, user_id, target.id)),
        affected_site_count=affected,
        already_tagged_count=overlap,
    )
