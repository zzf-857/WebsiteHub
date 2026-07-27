from __future__ import annotations

from typing import Literal

from sqlalchemy import and_, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.db.models import (
    Category,
    Site,
    utc_now,
)
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
) -> CategoryResponse:
    display, normalized = _display_name(name, maximum=80, field="分类名称")
    category = Category(user_id=user_id, name=display, normalized_name=normalized)
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
) -> CategoryResponse:
    category = await _owned_category(session, user_id, category_id)
    if category.is_default:
        raise LibraryConflictError("默认分类不能重命名")
    display, normalized = _display_name(name, maximum=80, field="分类名称")
    category.name = display
    category.normalized_name = normalized
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
    category = await _owned_category(session, user_id, category_id)
    if category.is_default:
        raise LibraryConflictError("默认分类不能删除")
    replacement = await _default_category(session, user_id)
    affected = await _category_count(session, user_id, category.id)
    await session.execute(
        update(Site)
        .where(Site.user_id == user_id, Site.category_id == category.id)
        .values(
            category_id=replacement.id,
            version=Site.version + 1,
            updated_at=utc_now(),
        )
    )
    await session.delete(category)
    await session.commit()
    return CategoryDeleteResponse(
        message="分类已删除，网站已迁移到未分类",
        reassigned_site_count=affected,
    )
