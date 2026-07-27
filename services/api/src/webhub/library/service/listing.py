from __future__ import annotations

import base64
import binascii
import hashlib
import json
import unicodedata
from datetime import datetime
from typing import Literal

from sqlalchemy import and_, case, exists, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.db.models import (
    Category,
    Site,
    SiteTag,
    SpaceMember,
    Tag,
)
from webhub.library.schemas import (
    SiteListAggregate,
    SiteListResponse,
)

from ._common import (
    _CJK_CHARACTER,
    _SEARCH_TOKEN,
    LibraryNotFoundError,
    LibraryValidationError,
    _owned_category,
    _owned_space,
    _owned_tag,
)
from .sites import (
    _site_response,
)

SortKey = Literal["created", "updated", "name", "custom"]
SortDirection = Literal["asc", "desc"]


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
        "custom": Site.position,
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
            cursor_value: str | int | datetime = (
                raw_value
                if sort == "name"
                else int(raw_value)
                if sort == "custom"
                else datetime.fromisoformat(raw_value)
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
        if sort == "name":
            raw_sort_value = last_site.normalized_name
        elif sort == "custom":
            raw_sort_value = str(last_site.position)
        else:
            raw_sort_value = (
                last_site.created_at if sort == "created" else last_site.updated_at
            ).isoformat()
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


async def reorder_sites(
    session: AsyncSession,
    user_id: str,
    category_id: str,
    *,
    ordered_site_ids: list[str],
    before_site_id: str | None,
) -> None:
    """Move one or more sites within a category, preserving relative order.

    ``before_site_id`` is the anchor the moved block lands in front of; ``None``
    means "send them to the end".  Anchoring beats an absolute index because an
    index computed from a list the user was looking at is stale the moment
    anything else changes, while "put these before that one" stays true.

    The two-pass write (shift everything out of range, then assign final
    positions) exists because SQLite has no deferred constraints: assigning
    final positions directly would collide with the unique index halfway
    through. Same shape as ``spaces.service.reorder_members``.
    """

    await _owned_category(session, user_id, category_id)
    if not ordered_site_ids:
        raise LibraryValidationError("重排至少需要一个网站")
    if len(set(ordered_site_ids)) != len(ordered_site_ids):
        raise LibraryValidationError("重排列表中存在重复网站")
    if before_site_id is not None and before_site_id in ordered_site_ids:
        raise LibraryValidationError("定位网站不能同时出现在移动列表中")

    rows = list(
        (
            await session.execute(
                select(Site.id, Site.position)
                .where(Site.user_id == user_id, Site.category_id == category_id)
                .order_by(Site.position, Site.id)
            )
        ).all()
    )
    current = [site_id for site_id, _ in rows]
    known = set(current)
    missing = [site_id for site_id in ordered_site_ids if site_id not in known]
    if missing or (before_site_id is not None and before_site_id not in known):
        raise LibraryNotFoundError("网站不在该分类中")

    moving = set(ordered_site_ids)
    remaining = [site_id for site_id in current if site_id not in moving]
    if before_site_id is None:
        final = [*remaining, *ordered_site_ids]
    else:
        anchor = remaining.index(before_site_id)
        final = [*remaining[:anchor], *ordered_site_ids, *remaining[anchor:]]

    if final == current:
        return

    offset = max((position for _, position in rows), default=-1) + 1 + len(final)
    await session.execute(
        update(Site)
        .where(Site.user_id == user_id, Site.category_id == category_id)
        .values(position=Site.position + offset)
        .execution_options(synchronize_session=False)
    )
    positions = {site_id: index for index, site_id in enumerate(final)}
    await session.execute(
        update(Site)
        .where(Site.user_id == user_id, Site.id.in_(final))
        .values(position=case(positions, value=Site.id))
        .execution_options(synchronize_session=False)
    )
    await session.commit()
