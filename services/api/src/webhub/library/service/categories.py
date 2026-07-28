from __future__ import annotations

from typing import Literal

from sqlalchemy import and_, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.db.locking import reserve_account_taxonomy
from webhub.db.models import (
    Category,
    Site,
    SiteMetadataPreference,
    utc_now,
)
from webhub.library.icons import infer_category_icon
from webhub.library.schemas import (
    CategoryDeletePreviewResponse,
    CategoryDeleteResponse,
    CategoryListResponse,
    CategoryResponse,
)

from ._common import (
    LibraryConflictError,
    _category_count,
    _category_response,
    _default_category,
    _display_name,
    _owned_category,
)

SortDirection = Literal["asc", "desc"]


async def list_categories(session: AsyncSession, user_id: str) -> CategoryListResponse:
    await _default_category(session, user_id)
    rows = (
        await session.execute(
            select(Category, func.count(Site.id))
            .outerjoin(
                Site,
                and_(Site.user_id == Category.user_id, Site.category_id == Category.id),
            )
            .where(Category.user_id == user_id)
            .group_by(Category.id)
            .order_by(Category.is_default.desc(), Category.normalized_name, Category.id)
        )
    ).all()
    return CategoryListResponse(
        items=[_category_response(category, int(count)) for category, count in rows]
    )


async def create_category(
    session: AsyncSession,
    user_id: str,
    name: str,
    icon: str | None = None,
) -> CategoryResponse:
    display, normalized = _display_name(name, maximum=80, field="分类名称")
    final_icon = icon.strip() if icon and icon.strip() else infer_category_icon(display)
    if not await reserve_account_taxonomy(session, user_id):
        raise LibraryConflictError("账号状态已发生变化，请刷新后重试")
    category = Category(user_id=user_id, name=display, normalized_name=normalized, icon=final_icon)
    session.add(category)
    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise LibraryConflictError("分类名称已存在") from error
    return _category_response(category, 0)


async def update_category(
    session: AsyncSession,
    user_id: str,
    category_id: str,
    name: str,
    icon: str | None = None,
) -> CategoryResponse:
    display, normalized = _display_name(name, maximum=80, field="分类名称")
    if not await reserve_account_taxonomy(session, user_id):
        raise LibraryConflictError("账号状态已发生变化，请刷新后重试")
    category = await _owned_category(session, user_id, category_id)
    if category.is_default:
        raise LibraryConflictError("默认分类不能重命名")
    category.name = display
    category.normalized_name = normalized
    # 与 Q3 确立的字段语义保持一致：**None = 别动，"" = 恢复默认（按名称推断）**。
    # 曾经这里在 icon 省略时也跑一遍 infer_category_icon，于是用户手选的图标会被
    # 一次纯重命名悄悄抹掉——改名和换图标是两个决定，不能互相牵连。
    if icon is not None:
        category.icon = icon.strip() or infer_category_icon(display)
    category.updated_at = utc_now()
    now = utc_now()
    try:
        await session.execute(
            update(Site)
            .where(Site.user_id == user_id, Site.category_id == category.id)
            .values(version=Site.version + 1, updated_at=now)
        )
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise LibraryConflictError("分类名称已存在") from error
    return _category_response(category, await _category_count(session, user_id, category.id))


async def category_delete_preview(
    session: AsyncSession,
    user_id: str,
    category_id: str,
) -> CategoryDeletePreviewResponse:
    category = await _owned_category(session, user_id, category_id)
    if category.is_default:
        raise LibraryConflictError("默认分类不能删除")
    replacement = await _default_category(session, user_id)
    affected = await _category_count(session, user_id, category.id)
    replacement_count = await _category_count(session, user_id, replacement.id)
    return CategoryDeletePreviewResponse(
        category=_category_response(category, affected),
        affected_site_count=affected,
        replacement_category=_category_response(replacement, replacement_count),
    )


async def delete_category(
    session: AsyncSession,
    user_id: str,
    category_id: str,
) -> CategoryDeleteResponse:
    if not await reserve_account_taxonomy(session, user_id):
        raise LibraryConflictError("账号状态已发生变化，请刷新后重试")
    category = await _owned_category(session, user_id, category_id)
    if category.is_default:
        raise LibraryConflictError("默认分类不能删除")
    replacement = await _default_category(session, user_id)
    # Lock both categories in one stable order before any Site row. Two
    # concurrent deletes therefore cannot take source/replacement locks in
    # opposite order, and no writer can move a new site into the source while
    # its membership snapshot is being migrated.
    for reserved_category_id in sorted({category.id, replacement.id}):
        reserved = await session.execute(
            update(Category)
            .where(
                Category.user_id == user_id,
                Category.id == reserved_category_id,
            )
            .values(updated_at=Category.updated_at)
        )
        if reserved.rowcount != 1:  # type: ignore[attr-defined]
            await session.rollback()
            raise LibraryConflictError(
                "分类已发生变化，请刷新后重试",
                code="category_conflict",
            )
    next_position = int(
        await session.scalar(
            select(func.coalesce(func.max(Site.position), -1) + 1).where(
                Site.user_id == user_id,
                Site.category_id == replacement.id,
            )
        )
        or 0
    )
    moved_site_statement = (
        select(Site.id)
        .where(Site.user_id == user_id, Site.category_id == category.id)
        .order_by(Site.position, Site.id)
    )
    if session.get_bind().dialect.name != "sqlite":
        moved_site_statement = moved_site_statement.with_for_update()
    moved_site_ids = list((await session.scalars(moved_site_statement)).all())
    affected = len(moved_site_ids)
    now = utc_now()
    for offset, site_id in enumerate(moved_site_ids):
        await session.execute(
            update(Site)
            .where(Site.user_id == user_id, Site.id == site_id)
            .values(
                category_id=replacement.id,
                position=next_position + offset,
                version=Site.version + 1,
                updated_at=now,
            )
        )
    if moved_site_ids:
        await session.execute(
            update(SiteMetadataPreference)
            .where(
                SiteMetadataPreference.user_id == user_id,
                SiteMetadataPreference.site_id.in_(moved_site_ids),
            )
            .values(
                category_is_manual=False,
                category_is_llm=False,
                llm_analyzed_at=None,
                updated_at=now,
            )
        )
    await session.delete(category)
    await session.commit()
    return CategoryDeleteResponse(
        message="分类已删除，网站已迁移到未分类",
        reassigned_site_count=affected,
    )
