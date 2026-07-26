from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable
from datetime import datetime
from typing import Literal

from sqlalchemy import and_, delete, exists, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.bookmarks.models import NormalizationStatus
from webhub.bookmarks.normalization import normalize_bookmark_url
from webhub.db.models import (
    DEFAULT_CATEGORY_NAME,
    Category,
    Site,
    SiteTag,
    Space,
    SpaceMember,
    Tag,
    utc_now,
)
from webhub.library.schemas import (
    CategoryDeletePreviewResponse,
    CategoryDeleteResponse,
    CategoryListResponse,
    CategoryReference,
    CategoryResponse,
    SiteCreateRequest,
    SiteDeleteResponse,
    SiteListAggregate,
    SiteListResponse,
    SiteResponse,
    SiteUpdateRequest,
    TagDeleteResponse,
    TagListResponse,
    TagReference,
    TagResponse,
    normalize_favicon_url,
)

SortKey = Literal["created", "updated", "name"]
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
            await session.scalars(
                select(Tag).where(Tag.user_id == user_id, Tag.id.in_(unique_ids))
            )
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


async def _site_response(
    session: AsyncSession,
    user_id: str,
    site: Site,
    category: Category | None = None,
    tags: list[Tag] | None = None,
) -> SiteResponse:
    selected_category = category or await _owned_category(
        session, user_id, site.category_id
    )
    selected_tags = tags
    if selected_tags is None:
        selected_tags = list(
            (
                await session.scalars(
                    select(Tag)
                    .join(
                        SiteTag,
                        and_(SiteTag.user_id == Tag.user_id, SiteTag.tag_id == Tag.id),
                    )
                    .where(
                        SiteTag.user_id == user_id,
                        SiteTag.site_id == site.id,
                    )
                    .order_by(Tag.normalized_name, Tag.id)
                )
            ).all()
        )
    return SiteResponse(
        id=site.id,
        name=site.name,
        original_url=site.original_url,
        identity_url=site.identity_url,
        description=site.description,
        favicon_url=_safe_favicon_url(site.favicon_url),
        category=CategoryReference(
            id=selected_category.id,
            name=selected_category.name,
            is_default=selected_category.is_default,
        ),
        tags=[TagReference(id=tag.id, name=tag.name) for tag in selected_tags],
        pinned=site.pinned,
        source=site.source,  # type: ignore[arg-type]
        analysis_status=site.analysis_status,  # type: ignore[arg-type]
        version=site.version,
        created_at=site.created_at,
        updated_at=site.updated_at,
    )


async def get_site(session: AsyncSession, user_id: str, site_id: str) -> SiteResponse:
    site = await _owned_site(session, user_id, site_id)
    return await _site_response(session, user_id, site)


async def create_site(
    session: AsyncSession,
    user_id: str,
    payload: SiteCreateRequest,
) -> SiteResponse:
    name, normalized_name = _display_name(payload.name, maximum=160, field="网站名称")
    original_url, identity_url = _site_url(payload.url)
    category = (
        await _owned_category(session, user_id, payload.category_id)
        if payload.category_id
        else await _default_category(session, user_id)
    )
    tags = await _owned_tags(session, user_id, payload.tag_ids)
    site = Site(
        user_id=user_id,
        category_id=category.id,
        name=name,
        normalized_name=normalized_name,
        original_url=original_url,
        identity_url=identity_url,
        description=payload.description.strip(),
        favicon_url=payload.favicon_url,
        pinned=payload.pinned,
    )
    session.add(site)
    try:
        await session.flush()
        session.add_all(
            SiteTag(user_id=user_id, site_id=site.id, tag_id=tag.id) for tag in tags
        )
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise LibraryConflictError(
            "该网址已存在于当前账号的资料库",
            code="duplicate_url",
        ) from error
    return await _site_response(session, user_id, site, category, tags)


async def update_site(
    session: AsyncSession,
    user_id: str,
    site_id: str,
    payload: SiteUpdateRequest,
) -> SiteResponse:
    site = await _owned_site(session, user_id, site_id)
    if site.version != payload.expected_version:
        raise LibraryConflictError(
            "网站已被修改，请刷新后重试",
            code="version_conflict",
        )

    fields = payload.model_fields_set - {"expected_version"}
    if not fields:
        raise LibraryValidationError("网站更新至少需要一个字段")

    name_update: tuple[str, str] | None = None
    if "name" in fields:
        if payload.name is None:
            raise LibraryValidationError("网站名称不能为空")
        name_update = _display_name(
            payload.name, maximum=160, field="网站名称"
        )

    url_update: tuple[str, str] | None = None
    if "url" in fields:
        if payload.url is None:
            raise LibraryValidationError("网址不能为空")
        url_update = _site_url(payload.url)

    if "pinned" in fields and payload.pinned is None:
        raise LibraryValidationError("置顶状态不能为空")

    category: Category | None = None
    if "category_id" in fields:
        category = (
            await _owned_category(session, user_id, payload.category_id)
            if payload.category_id
            else await _default_category(session, user_id)
        )

    tags: list[Tag] | None = None
    if "tag_ids" in fields:
        if payload.tag_ids is None:
            raise LibraryValidationError("标签列表不能为空")
        tags = await _owned_tags(session, user_id, payload.tag_ids)

    try:
        claimed_at = utc_now()
        claim = await session.execute(
            update(Site)
            .where(
                Site.user_id == user_id,
                Site.id == site_id,
                Site.version == payload.expected_version,
            )
            .values(version=Site.version + 1, updated_at=claimed_at)
            .execution_options(synchronize_session=False)
        )
        if claim.rowcount != 1:  # type: ignore[attr-defined]
            await session.rollback()
            raise LibraryConflictError(
                "网站已被修改，请刷新后重试",
                code="version_conflict",
            )

        await session.refresh(site)
        if name_update is not None:
            site.name, site.normalized_name = name_update
        if url_update is not None:
            site.original_url, site.identity_url = url_update
        if "description" in fields:
            site.description = (payload.description or "").strip()
        if "favicon_url" in fields:
            site.favicon_url = payload.favicon_url
        if "pinned" in fields:
            site.pinned = bool(payload.pinned)
        if category is not None:
            site.category_id = category.id
        if tags is not None:
            await session.execute(
                delete(SiteTag).where(
                    SiteTag.user_id == user_id,
                    SiteTag.site_id == site.id,
                )
            )
            session.add_all(
                SiteTag(user_id=user_id, site_id=site.id, tag_id=tag.id) for tag in tags
            )

        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise LibraryConflictError(
            "该网址已存在于当前账号的资料库",
            code="duplicate_url",
        ) from error
    return await _site_response(session, user_id, site)


async def delete_site(
    session: AsyncSession,
    user_id: str,
    site_id: str,
    *,
    expected_version: int,
) -> SiteDeleteResponse:
    site = await _owned_site(session, user_id, site_id)
    if site.version != expected_version:
        raise LibraryConflictError(
            "网站已被修改，请刷新后重试",
            code="version_conflict",
        )
    now = utc_now()
    related_space_ids = list(
        (
            await session.scalars(
                select(SpaceMember.space_id).where(
                    SpaceMember.user_id == user_id,
                    SpaceMember.site_id == site_id,
                )
            )
        ).all()
    )
    deleted = await session.execute(
        delete(Site)
        .where(
            Site.user_id == user_id,
            Site.id == site_id,
            Site.version == expected_version,
        )
        .execution_options(synchronize_session=False)
    )
    if deleted.rowcount != 1:  # type: ignore[attr-defined]
        await session.rollback()
        raise LibraryConflictError(
            "网站已被修改，请刷新后重试",
            code="version_conflict",
        )
    if related_space_ids:
        await session.execute(
            update(Space)
            .where(Space.user_id == user_id, Space.id.in_(related_space_ids))
            .values(version=Space.version + 1, updated_at=now)
        )
    await session.commit()
    return SiteDeleteResponse(message="网站已删除", site_id=site_id)


def _cursor_scope(
    *,
    user_id: str,
    q: str | None,
    category_id: str | None,
    tag_id: str | None,
    space_id: str | None,
    space_version: int | None,
    pinned: bool | None,
    sort: SortKey,
    direction: SortDirection,
) -> str:
    scope_payload: dict[str, object] = {
        "user_id": user_id,
        "q": (q or "").strip(),
        "category_id": category_id,
        "tag_id": tag_id,
        "space_id": space_id,
        "pinned": pinned,
        "sort": sort,
        "direction": direction,
    }
    if space_id is not None:
        scope_payload["space_version"] = space_version
    payload = json.dumps(
        scope_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:20]


def _encode_cursor(
    *,
    sort: SortKey,
    direction: SortDirection,
    value: str,
    site_id: str,
    scope: str,
) -> str:
    payload = json.dumps(
        {
            "v": 1,
            "sort": sort,
            "direction": direction,
            "value": value,
            "id": site_id,
            "scope": scope,
        },
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(
    cursor: str,
    *,
    sort: SortKey,
    direction: SortDirection,
    scope: str,
) -> tuple[str, str]:
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(cursor + padding))
        if (
            not isinstance(payload, dict)
            or payload.get("v") != 1
            or payload.get("sort") != sort
            or payload.get("direction") != direction
            or payload.get("scope") != scope
            or not isinstance(payload.get("value"), str)
            or not isinstance(payload.get("id"), str)
        ):
            raise ValueError
        return payload["value"], payload["id"]
    except (
        binascii.Error,
        UnicodeDecodeError,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
    ) as error:
        raise LibraryValidationError("分页游标无效或与当前筛选条件不匹配") from error


def _search_tokens(query: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", query).casefold()
    return _SEARCH_TOKEN.findall(normalized)[:12]


def _fts_query(query: str) -> str | None:
    tokens = _search_tokens(query)
    if not tokens:
        return None
    return " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)


def _like_token_condition(user_id: str, token: str):
    escaped = token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    tag_match = exists(
        select(SiteTag.site_id)
        .join(
            Tag,
            and_(Tag.user_id == SiteTag.user_id, Tag.id == SiteTag.tag_id),
        )
        .where(
            SiteTag.user_id == user_id,
            SiteTag.site_id == Site.id,
            func.lower(Tag.name).like(pattern, escape="\\"),
        )
    )
    return or_(
        func.lower(Site.name).like(pattern, escape="\\"),
        func.lower(Site.original_url).like(pattern, escape="\\"),
        func.lower(Site.identity_url).like(pattern, escape="\\"),
        func.lower(Site.description).like(pattern, escape="\\"),
        func.lower(Category.name).like(pattern, escape="\\"),
        tag_match,
    )


def _search_condition(user_id: str, query: str):
    tokens = _search_tokens(query)
    if not tokens:
        normalized_query = unicodedata.normalize("NFKC", query).casefold()
        return _like_token_condition(user_id, normalized_query)
    if any(len(token) <= 2 or _CJK_CHARACTER.search(token) for token in tokens):
        return and_(*(_like_token_condition(user_id, token) for token in tokens))
    fts_query = _fts_query(query)
    if fts_query is None:
        raise AssertionError("non-empty search tokens must produce an FTS query")
    return text(
        "sites.id IN ("
        "SELECT site_id FROM site_search "
        "WHERE user_id = :fts_user_id AND site_search MATCH :fts_query"
        ")"
    ).bindparams(fts_user_id=user_id, fts_query=fts_query)


def _site_filters(
    *,
    user_id: str,
    q: str | None,
    category_id: str | None,
    tag_id: str | None,
    space_id: str | None,
    pinned: bool | None,
):
    conditions = [Site.user_id == user_id]
    if q and (query := q.strip()):
        conditions.append(_search_condition(user_id, query))
    if category_id:
        conditions.append(Site.category_id == category_id)
    if tag_id:
        conditions.append(
            exists(
                select(SiteTag.site_id).where(
                    SiteTag.user_id == user_id,
                    SiteTag.site_id == Site.id,
                    SiteTag.tag_id == tag_id,
                )
            )
        )
    if space_id:
        conditions.append(
            exists(
                select(SpaceMember.site_id).where(
                    SpaceMember.user_id == user_id,
                    SpaceMember.space_id == space_id,
                    SpaceMember.site_id == Site.id,
                )
            )
        )
    if pinned is not None:
        conditions.append(Site.pinned.is_(pinned))
    return conditions


async def list_sites(
    session: AsyncSession,
    user_id: str,
    *,
    q: str | None,
    category_id: str | None,
    tag_id: str | None,
    space_id: str | None,
    pinned: bool | None,
    sort: SortKey,
    direction: SortDirection,
    cursor: str | None,
    limit: int,
) -> SiteListResponse:
    space_version: int | None = None
    if category_id:
        await _owned_category(session, user_id, category_id)
    if tag_id:
        await _owned_tag(session, user_id, tag_id)
    if space_id:
        space = await _owned_space(session, user_id, space_id)
        space_version = space.version

    filters = _site_filters(
        user_id=user_id,
        q=q,
        category_id=category_id,
        tag_id=tag_id,
        space_id=space_id,
        pinned=pinned,
    )
    matched_count = int(
        await session.scalar(
            select(func.count(Site.id)).join(
                Category,
                and_(Category.user_id == Site.user_id, Category.id == Site.category_id),
            ).where(*filters)
        )
        or 0
    )
    pinned_filters = _site_filters(
        user_id=user_id,
        q=q,
        category_id=category_id,
        tag_id=tag_id,
        space_id=space_id,
        pinned=True,
    )
    pinned_count = int(
        await session.scalar(
            select(func.count(Site.id)).join(
                Category,
                and_(Category.user_id == Site.user_id, Category.id == Site.category_id),
            ).where(*pinned_filters)
        )
        or 0
    )

    sort_column = {
        "created": Site.created_at,
        "updated": Site.updated_at,
        "name": Site.normalized_name,
    }[sort]
    scope = _cursor_scope(
        user_id=user_id,
        q=q,
        category_id=category_id,
        tag_id=tag_id,
        space_id=space_id,
        space_version=space_version,
        pinned=pinned,
        sort=sort,
        direction=direction,
    )
    query = select(Site, Category).join(
        Category,
        and_(Category.user_id == Site.user_id, Category.id == Site.category_id),
    ).where(*filters)
    if cursor:
        raw_value, cursor_id = _decode_cursor(
            cursor,
            sort=sort,
            direction=direction,
            scope=scope,
        )
        try:
            cursor_value: str | datetime = (
                raw_value if sort == "name" else datetime.fromisoformat(raw_value)
            )
        except ValueError as error:
            raise LibraryValidationError("分页游标包含无效排序值") from error
        comparator = (
            or_(
                sort_column > cursor_value,
                and_(sort_column == cursor_value, Site.id > cursor_id),
            )
            if direction == "asc"
            else or_(
                sort_column < cursor_value,
                and_(sort_column == cursor_value, Site.id < cursor_id),
            )
        )
        query = query.where(comparator)
    ordering = sort_column.asc() if direction == "asc" else sort_column.desc()
    id_ordering = Site.id.asc() if direction == "asc" else Site.id.desc()
    rows = (await session.execute(query.order_by(ordering, id_ordering).limit(limit + 1))).all()
    has_more = len(rows) > limit
    selected_rows = rows[:limit]

    site_ids = [site.id for site, _ in selected_rows]
    tags_by_site: dict[str, list[Tag]] = {site_id: [] for site_id in site_ids}
    if site_ids:
        tag_rows = (
            await session.execute(
                select(SiteTag.site_id, Tag)
                .join(
                    Tag,
                    and_(Tag.user_id == SiteTag.user_id, Tag.id == SiteTag.tag_id),
                )
                .where(SiteTag.user_id == user_id, SiteTag.site_id.in_(site_ids))
                .order_by(SiteTag.site_id, Tag.normalized_name, Tag.id)
            )
        ).all()
        for site_id, tag in tag_rows:
            tags_by_site[site_id].append(tag)

    items = [
        await _site_response(session, user_id, site, category, tags_by_site[site.id])
        for site, category in selected_rows
    ]
    next_cursor = None
    if has_more and selected_rows:
        last_site = selected_rows[-1][0]
        raw_sort_value = (
            last_site.normalized_name
            if sort == "name"
            else (last_site.created_at if sort == "created" else last_site.updated_at).isoformat()
        )
        next_cursor = _encode_cursor(
            sort=sort,
            direction=direction,
            value=raw_sort_value,
            site_id=last_site.id,
            scope=scope,
        )

    return SiteListResponse(
        items=items,
        next_cursor=next_cursor,
        aggregate=SiteListAggregate(
            matched_count=matched_count,
            pinned_count=pinned_count,
        ),
    )
