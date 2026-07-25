from __future__ import annotations

import base64
import binascii
import hashlib
import json
import unicodedata
from datetime import datetime
from typing import Literal

from sqlalchemy import and_, case, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.db.models import Site, Space, SpaceMember, utc_now
from webhub.spaces.schemas import (
    SpaceCreateRequest,
    SpaceDeletePreviewResponse,
    SpaceDeleteResponse,
    SpaceDetailResponse,
    SpaceListAggregate,
    SpaceListResponse,
    SpaceMemberAddRequest,
    SpaceMemberAddResponse,
    SpaceMemberDeleteResponse,
    SpaceMemberResponse,
    SpaceReorderRequest,
    SpaceResponse,
    SpaceSiteReference,
    SpaceUpdateRequest,
)

SortKey = Literal["created", "updated", "name"]
SortDirection = Literal["asc", "desc"]
_REORDER_UPDATE_CHUNK = 200


class SpaceError(Exception):
    status_code = 400

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class SpaceNotFoundError(SpaceError):
    status_code = 404


class SpaceConflictError(SpaceError):
    status_code = 409


class SpaceValidationError(SpaceError):
    status_code = 422


def _space_name(value: str) -> tuple[str, str]:
    display = " ".join(unicodedata.normalize("NFKC", value).split())
    if not display:
        raise SpaceValidationError("Space 名称不能为空")
    if len(display) > 120:
        raise SpaceValidationError("Space 名称不能超过 120 个字符")
    return display, display.casefold()


async def _owned_space(session: AsyncSession, user_id: str, space_id: str) -> Space:
    space = await session.scalar(
        select(Space).where(Space.user_id == user_id, Space.id == space_id)
    )
    if space is None:
        raise SpaceNotFoundError("Space 不存在")
    return space


async def _owned_site(session: AsyncSession, user_id: str, site_id: str) -> Site:
    site = await session.scalar(select(Site).where(Site.user_id == user_id, Site.id == site_id))
    if site is None:
        raise SpaceNotFoundError("网站不存在")
    return site


async def _owned_member(
    session: AsyncSession,
    user_id: str,
    space_id: str,
    site_id: str,
) -> SpaceMember:
    member = await session.scalar(
        select(SpaceMember).where(
            SpaceMember.user_id == user_id,
            SpaceMember.space_id == space_id,
            SpaceMember.site_id == site_id,
        )
    )
    if member is None:
        raise SpaceNotFoundError("Space 成员不存在")
    return member


async def _member_count(session: AsyncSession, user_id: str, space_id: str) -> int:
    return int(
        await session.scalar(
            select(func.count(SpaceMember.site_id)).where(
                SpaceMember.user_id == user_id,
                SpaceMember.space_id == space_id,
            )
        )
        or 0
    )


def _space_response(space: Space, member_count: int) -> SpaceResponse:
    return SpaceResponse(
        id=space.id,
        name=space.name,
        member_count=member_count,
        version=space.version,
        created_at=space.created_at,
        updated_at=space.updated_at,
    )


def _member_response(member: SpaceMember, site: Site) -> SpaceMemberResponse:
    return SpaceMemberResponse(
        site=SpaceSiteReference(
            id=site.id,
            name=site.name,
            original_url=site.original_url,
            identity_url=site.identity_url,
            description=site.description,
            favicon_url=site.favicon_url,
            pinned=site.pinned,
            version=site.version,
        ),
        position=member.position,
        added_at=member.created_at,
    )


async def _claim_version(
    session: AsyncSession,
    space: Space,
    expected_version: int,
) -> None:
    if space.version != expected_version:
        raise SpaceConflictError("Space 已被修改，请刷新后重试")
    result = await session.execute(
        update(Space)
        .where(
            Space.user_id == space.user_id,
            Space.id == space.id,
            Space.version == expected_version,
        )
        .values(version=Space.version + 1, updated_at=utc_now())
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        await session.rollback()
        raise SpaceConflictError("Space 已被修改，请刷新后重试")
    await session.refresh(space)


def _cursor_scope(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode()).hexdigest()[:20]


def _encode_cursor(*, kind: str, value: str, item_id: str, scope: str) -> str:
    payload = json.dumps(
        {"v": 1, "kind": kind, "value": value, "id": item_id, "scope": scope},
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(
    cursor: str,
    *,
    kind: str,
    scope: str,
) -> tuple[str, str]:
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(cursor + padding))
        if (
            not isinstance(payload, dict)
            or payload.get("v") != 1
            or payload.get("kind") != kind
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
        raise SpaceValidationError("分页游标无效或与当前资源不匹配") from error


async def list_spaces(
    session: AsyncSession,
    user_id: str,
    *,
    sort: SortKey,
    direction: SortDirection,
    cursor: str | None,
    limit: int,
) -> SpaceListResponse:
    total_count = int(
        await session.scalar(select(func.count(Space.id)).where(Space.user_id == user_id)) or 0
    )
    sort_column = {
        "created": Space.created_at,
        "updated": Space.updated_at,
        "name": Space.normalized_name,
    }[sort]
    scope = _cursor_scope({"user_id": user_id, "sort": sort, "direction": direction})
    count_query = (
        select(func.count(SpaceMember.site_id))
        .where(
            SpaceMember.user_id == Space.user_id,
            SpaceMember.space_id == Space.id,
        )
        .correlate(Space)
        .scalar_subquery()
    )
    query = select(Space, count_query).where(Space.user_id == user_id)
    if cursor:
        raw_value, cursor_id = _decode_cursor(cursor, kind="spaces", scope=scope)
        try:
            cursor_value: str | datetime = (
                raw_value if sort == "name" else datetime.fromisoformat(raw_value)
            )
        except ValueError as error:
            raise SpaceValidationError("分页游标包含无效排序值") from error
        comparator = (
            or_(
                sort_column > cursor_value,
                and_(sort_column == cursor_value, Space.id > cursor_id),
            )
            if direction == "asc"
            else or_(
                sort_column < cursor_value,
                and_(sort_column == cursor_value, Space.id < cursor_id),
            )
        )
        query = query.where(comparator)
    ordering = sort_column.asc() if direction == "asc" else sort_column.desc()
    id_ordering = Space.id.asc() if direction == "asc" else Space.id.desc()
    rows = (await session.execute(query.order_by(ordering, id_ordering).limit(limit + 1))).all()
    selected_rows = rows[:limit]
    next_cursor = None
    if len(rows) > limit and selected_rows:
        last_space = selected_rows[-1][0]
        raw_sort_value = (
            last_space.normalized_name
            if sort == "name"
            else (last_space.created_at if sort == "created" else last_space.updated_at).isoformat()
        )
        next_cursor = _encode_cursor(
            kind="spaces",
            value=raw_sort_value,
            item_id=last_space.id,
            scope=scope,
        )
    return SpaceListResponse(
        items=[_space_response(space, int(member_count)) for space, member_count in selected_rows],
        next_cursor=next_cursor,
        aggregate=SpaceListAggregate(total_count=total_count),
    )


async def create_space(
    session: AsyncSession,
    user_id: str,
    payload: SpaceCreateRequest,
) -> SpaceResponse:
    name, normalized_name = _space_name(payload.name)
    space = Space(user_id=user_id, name=name, normalized_name=normalized_name)
    session.add(space)
    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise SpaceConflictError("Space 名称已存在") from error
    return _space_response(space, 0)


async def get_space(
    session: AsyncSession,
    user_id: str,
    space_id: str,
    *,
    cursor: str | None,
    limit: int,
) -> SpaceDetailResponse:
    space = await _owned_space(session, user_id, space_id)
    member_count = await _member_count(session, user_id, space_id)
    scope = _cursor_scope({"user_id": user_id, "space_id": space_id, "version": space.version})
    query = (
        select(SpaceMember, Site)
        .join(
            Site,
            and_(
                Site.user_id == SpaceMember.user_id,
                Site.id == SpaceMember.site_id,
            ),
        )
        .where(
            SpaceMember.user_id == user_id,
            SpaceMember.space_id == space_id,
        )
    )
    if cursor:
        raw_position, cursor_site_id = _decode_cursor(cursor, kind="space-members", scope=scope)
        try:
            cursor_position = int(raw_position)
        except ValueError as error:
            raise SpaceValidationError("分页游标包含无效成员位置") from error
        query = query.where(
            or_(
                SpaceMember.position > cursor_position,
                and_(
                    SpaceMember.position == cursor_position,
                    SpaceMember.site_id > cursor_site_id,
                ),
            )
        )
    rows = (
        await session.execute(
            query.order_by(SpaceMember.position, SpaceMember.site_id).limit(limit + 1)
        )
    ).all()
    selected_rows = rows[:limit]
    next_cursor = None
    if len(rows) > limit and selected_rows:
        last_member = selected_rows[-1][0]
        next_cursor = _encode_cursor(
            kind="space-members",
            value=str(last_member.position),
            item_id=last_member.site_id,
            scope=scope,
        )
    summary = _space_response(space, member_count)
    return SpaceDetailResponse(
        **summary.model_dump(),
        members=[_member_response(member, site) for member, site in selected_rows],
        next_cursor=next_cursor,
    )


async def update_space(
    session: AsyncSession,
    user_id: str,
    space_id: str,
    payload: SpaceUpdateRequest,
) -> SpaceResponse:
    space = await _owned_space(session, user_id, space_id)
    name, normalized_name = _space_name(payload.name)
    try:
        await _claim_version(session, space, payload.expected_version)
        space.name = name
        space.normalized_name = normalized_name
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise SpaceConflictError("Space 名称已存在") from error
    return _space_response(space, await _member_count(session, user_id, space_id))


async def add_member(
    session: AsyncSession,
    user_id: str,
    space_id: str,
    payload: SpaceMemberAddRequest,
) -> SpaceMemberAddResponse:
    space = await _owned_space(session, user_id, space_id)
    site = await _owned_site(session, user_id, payload.site_id)
    existing = await session.scalar(
        select(SpaceMember.site_id).where(
            SpaceMember.user_id == user_id,
            SpaceMember.space_id == space_id,
            SpaceMember.site_id == site.id,
        )
    )
    if existing is not None:
        raise SpaceConflictError("网站已在该 Space 中")

    try:
        await _claim_version(session, space, payload.expected_version)
        next_position = int(
            await session.scalar(
                select(func.coalesce(func.max(SpaceMember.position), -1) + 1).where(
                    SpaceMember.user_id == user_id,
                    SpaceMember.space_id == space_id,
                )
            )
            or 0
        )
        member = SpaceMember(
            user_id=user_id,
            space_id=space_id,
            site_id=site.id,
            position=next_position,
        )
        session.add(member)
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise SpaceConflictError("Space 成员关系已发生变化，请刷新后重试") from error

    member_count = await _member_count(session, user_id, space_id)
    return SpaceMemberAddResponse(
        space=_space_response(space, member_count),
        member=_member_response(member, site),
    )


async def remove_member(
    session: AsyncSession,
    user_id: str,
    space_id: str,
    site_id: str,
    *,
    expected_version: int,
) -> SpaceMemberDeleteResponse:
    space = await _owned_space(session, user_id, space_id)
    member = await _owned_member(session, user_id, space_id, site_id)
    await _claim_version(session, space, expected_version)
    await session.delete(member)
    await session.commit()
    return SpaceMemberDeleteResponse(
        message="网站已从 Space 移除",
        space_id=space_id,
        site_id=site_id,
        member_count=await _member_count(session, user_id, space_id),
        version=space.version,
    )


async def reorder_members(
    session: AsyncSession,
    user_id: str,
    space_id: str,
    payload: SpaceReorderRequest,
) -> SpaceResponse:
    space = await _owned_space(session, user_id, space_id)
    if space.version != payload.expected_version:
        raise SpaceConflictError("Space 已被修改，请刷新后重试")

    current_rows = (
        await session.execute(
            select(SpaceMember.site_id, SpaceMember.position)
            .where(
                SpaceMember.user_id == user_id,
                SpaceMember.space_id == space_id,
            )
            .order_by(SpaceMember.position, SpaceMember.site_id)
        )
    ).all()
    current_ids = [site_id for site_id, _ in current_rows]
    current_set = set(current_ids)
    if any(site_id not in current_set for site_id in payload.ordered_site_ids):
        raise SpaceNotFoundError("Space 成员不存在")
    if payload.before_site_id is not None and payload.before_site_id not in current_set:
        raise SpaceNotFoundError("排序定位成员不存在")

    moved = set(payload.ordered_site_ids)
    remaining = [site_id for site_id in current_ids if site_id not in moved]
    insert_at = (
        len(remaining)
        if payload.before_site_id is None
        else remaining.index(payload.before_site_id)
    )
    reordered = remaining[:insert_at] + payload.ordered_site_ids + remaining[insert_at:]
    try:
        await _claim_version(session, space, payload.expected_version)
        if reordered != current_ids:
            offset = max(position for _, position in current_rows) + 1
            await session.execute(
                update(SpaceMember)
                .where(
                    SpaceMember.user_id == user_id,
                    SpaceMember.space_id == space_id,
                )
                .values(position=SpaceMember.position + offset)
                .execution_options(synchronize_session=False)
            )
            for start in range(0, len(reordered), _REORDER_UPDATE_CHUNK):
                chunk = reordered[start : start + _REORDER_UPDATE_CHUNK]
                positions = {
                    site_id: position for position, site_id in enumerate(chunk, start=start)
                }
                await session.execute(
                    update(SpaceMember)
                    .where(
                        SpaceMember.user_id == user_id,
                        SpaceMember.space_id == space_id,
                        SpaceMember.site_id.in_(chunk),
                    )
                    .values(position=case(positions, value=SpaceMember.site_id))
                    .execution_options(synchronize_session=False)
                )
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise SpaceConflictError("Space 成员顺序已发生变化，请刷新后重试") from error
    return _space_response(space, len(reordered))


async def delete_preview(
    session: AsyncSession,
    user_id: str,
    space_id: str,
) -> SpaceDeletePreviewResponse:
    space = await _owned_space(session, user_id, space_id)
    member_count = await _member_count(session, user_id, space_id)
    summary = _space_response(space, member_count)
    return SpaceDeletePreviewResponse(
        space=summary,
        affected_site_count=member_count,
    )


async def delete_space(
    session: AsyncSession,
    user_id: str,
    space_id: str,
    *,
    expected_version: int,
) -> SpaceDeleteResponse:
    space = await _owned_space(session, user_id, space_id)
    if space.version != expected_version:
        raise SpaceConflictError("Space 已被修改，请刷新后重试")
    member_count = await _member_count(session, user_id, space_id)
    result = await session.execute(
        delete(Space)
        .where(
            Space.user_id == user_id,
            Space.id == space_id,
            Space.version == expected_version,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        await session.rollback()
        raise SpaceConflictError("Space 已被修改，请刷新后重试")
    await session.commit()
    return SpaceDeleteResponse(
        message="Space 已删除，网站仍保留在资料库中",
        space_id=space_id,
        unlinked_site_count=member_count,
    )
