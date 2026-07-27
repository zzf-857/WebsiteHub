from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.bookmarks.models import NormalizationStatus
from webhub.bookmarks.normalization import normalize_bookmark_url
from webhub.db.models import (
    DEFAULT_CATEGORY_NAME,
    Category,
    Site,
    Space,
    Tag,
)
from webhub.library.schemas import (
    CategoryResponse,
    TagResponse,
    normalize_favicon_url,
)

# "relevance" 只在带 q 时可用，且是唯一会走语义融合的排序。
# 单独开一个键而不是让语义召回去改现有排序的结果顺序：现有四个键的含义
# 是"按这一列排"，把融合塞进去会让同一个请求在配了 Provider 后悄悄换个顺序。
SortKey = Literal["created", "updated", "name", "custom", "relevance"]
SortDirection = Literal["asc", "desc"]
_SEARCH_TOKEN = re.compile(r"\w+", re.UNICODE)
_CJK_CHARACTER = re.compile(r"[\u3400-\u9fff]")


class LibraryError(Exception):
    status_code = 400
    code = "library_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        self.message = message
        self.code = code or type(self).code
        super().__init__(message)


class LibraryNotFoundError(LibraryError):
    status_code = 404
    code = "not_found"


class LibraryConflictError(LibraryError):
    status_code = 409
    code = "conflict"


class LibraryValidationError(LibraryError):
    status_code = 422
    code = "validation_error"


def _display_name(value: str, *, maximum: int, field: str) -> tuple[str, str]:
    display = " ".join(unicodedata.normalize("NFKC", value).split())
    if not display:
        raise LibraryValidationError(f"{field}不能为空")
    if len(display) > maximum:
        raise LibraryValidationError(f"{field}不能超过 {maximum} 个字符")
    return display, display.casefold()


def _site_url(value: str) -> tuple[str, str]:
    original_url = value.strip()
    normalized = normalize_bookmark_url(original_url)
    if normalized.status is not NormalizationStatus.ACCEPTED or not normalized.normalized_url:
        reason = normalized.reason or "invalid_url"
        raise LibraryValidationError(f"网址无效或不受支持：{reason}")
    return original_url, normalized.normalized_url


def _safe_favicon_url(value: str | None) -> str | None:
    try:
        normalized = normalize_favicon_url(value)
    except ValueError:
        return None
    return normalized if isinstance(normalized, str) else None


# 注意：**绝不合成第三方 CDN 地址。** 曾经这里有个 resolve_favicon_url，在站点没有
# 图标时回落到 `https://www.google.com/s2/favicons?domain=...`，三处不可接受：
# ① 违反项目硬约束「favicon 不走第三方 CDN」；② 用户书签库里的每个域名都会被逐个
# 透露给第三方，是隐式的浏览历史泄露；③ 它把 favicon_url 永久填满，使 ingestion
# 那条「只补空字段」的抓取规则永远不触发——真实抓到的图标反而写不进去。
# 没有图标时返回 None 才是正确答案：前端 SiteFavicon 用站点名首字符渲染本地字母块，
# 不需要任何出站请求。


async def _default_category(session: AsyncSession, user_id: str) -> Category:
    category = await session.scalar(
        select(Category).where(Category.user_id == user_id, Category.is_default.is_(True))
    )
    if category is None:
        category = Category(
            user_id=user_id,
            name=DEFAULT_CATEGORY_NAME,
            normalized_name=DEFAULT_CATEGORY_NAME,
            is_default=True,
        )
        session.add(category)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            category = await session.scalar(
                select(Category).where(
                    Category.user_id == user_id,
                    Category.is_default.is_(True),
                )
            )
            if category is None:
                raise
    return category


async def _owned_category(session: AsyncSession, user_id: str, category_id: str) -> Category:
    category = await session.scalar(
        select(Category).where(Category.user_id == user_id, Category.id == category_id)
    )
    if category is None:
        raise LibraryNotFoundError("分类不存在")
    return category


async def _owned_tag(session: AsyncSession, user_id: str, tag_id: str) -> Tag:
    tag = await session.scalar(select(Tag).where(Tag.user_id == user_id, Tag.id == tag_id))
    if tag is None:
        raise LibraryNotFoundError("标签不存在")
    return tag


async def _owned_tags(
    session: AsyncSession,
    user_id: str,
    tag_ids: Iterable[str],
) -> list[Tag]:
    unique_ids = list(dict.fromkeys(tag_ids))
    if len(unique_ids) > 50:
        raise LibraryValidationError("单个网站最多关联 50 个标签")
    if not unique_ids:
        return []
    tags = list(
        (
            await session.scalars(select(Tag).where(Tag.user_id == user_id, Tag.id.in_(unique_ids)))
        ).all()
    )
    if len(tags) != len(unique_ids):
        raise LibraryNotFoundError("标签不存在")
    by_id = {tag.id: tag for tag in tags}
    return [by_id[tag_id] for tag_id in unique_ids]


async def _owned_site(session: AsyncSession, user_id: str, site_id: str) -> Site:
    site = await session.scalar(select(Site).where(Site.user_id == user_id, Site.id == site_id))
    if site is None:
        raise LibraryNotFoundError("网站不存在")
    return site


async def _owned_space(session: AsyncSession, user_id: str, space_id: str) -> Space:
    space = await session.scalar(
        select(Space).where(Space.user_id == user_id, Space.id == space_id)
    )
    if space is None:
        raise LibraryNotFoundError("Space 不存在")
    return space


async def _category_count(session: AsyncSession, user_id: str, category_id: str) -> int:
    return int(
        await session.scalar(
            select(func.count(Site.id)).where(
                Site.user_id == user_id,
                Site.category_id == category_id,
            )
        )
        or 0
    )


def _category_response(category: Category, site_count: int) -> CategoryResponse:
    return CategoryResponse(
        id=category.id,
        name=category.name,
        is_default=category.is_default,
        icon=category.icon,
        site_count=site_count,
        created_at=category.created_at,
        updated_at=category.updated_at,
    )


def _tag_response(tag: Tag, site_count: int) -> TagResponse:
    return TagResponse(
        id=tag.id,
        name=tag.name,
        site_count=site_count,
        created_at=tag.created_at,
        updated_at=tag.updated_at,
    )
