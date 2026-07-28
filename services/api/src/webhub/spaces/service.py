from __future__ import annotations

import base64
import binascii
import hashlib
import json
import unicodedata
from datetime import datetime
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import and_, case, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.db.models import (
    Site,
    Space,
    SpaceBatchOperationReceipt,
    SpaceMember,
    utc_now,
)
from webhub.library.schemas import normalize_favicon_url
from webhub.spaces.schemas import (
    SpaceCreateRequest,
    SpaceDeletePreviewResponse,
    SpaceDeleteResponse,
    SpaceDetailResponse,
    SpaceListAggregate,
    SpaceListResponse,
    SpaceMemberAddRequest,
    SpaceMemberAddResponse,
    SpaceMemberBatchRequest,
    SpaceMemberBatchResponse,
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
    code = "space_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        self.message = message
        self.code = code or type(self).code
        super().__init__(message)


class SpaceNotFoundError(SpaceError):
    status_code = 404
    code = "not_found"


class SpaceConflictError(SpaceError):
    status_code = 409
    code = "conflict"


class SpaceValidationError(SpaceError):
    status_code = 422
    code = "validation_error"


def _space_name(value: str) -> tuple[str, str]:
    display = " ".join(unicodedata.normalize("NFKC", value).split())
    if not display:
        raise SpaceValidationError("Space 名称不能为空")
    if len(display) > 120:
        raise SpaceValidationError("Space 名称不能超过 120 个字符")
    return display, display.casefold()


def space_batch_space_id(user_id: str, operation_id: str) -> str:
    """Derive the resource identity for one account-scoped create operation."""

    identity = f"webhub:space-batch:{user_id}:{operation_id.strip()}"
    return str(uuid5(NAMESPACE_URL, identity))


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
        raise SpaceNotFoundError("Space 成员不存在", code="member_not_found")
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


def _safe_favicon_url(value: str | None) -> str | None:
    try:
        normalized = normalize_favicon_url(value)
    except ValueError:
        return None
    return normalized if isinstance(normalized, str) else None


def _member_response(member: SpaceMember, site: Site) -> SpaceMemberResponse:
    return SpaceMemberResponse(
        site=SpaceSiteReference(
            id=site.id,
            name=site.name,
            original_url=site.original_url,
            identity_url=site.identity_url,
            summary=site.summary,
            description=site.description,
            favicon_url=_safe_favicon_url(site.favicon_url),
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
        raise SpaceConflictError(
            "Space 已被修改，请刷新后重试",
            code="version_conflict",
        )
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
        raise SpaceConflictError(
            "Space 已被修改，请刷新后重试",
            code="version_conflict",
        )
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
        raise SpaceConflictError("Space 名称已存在", code="duplicate_name") from error
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
        raise SpaceConflictError("Space 名称已存在", code="duplicate_name") from error
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
        raise SpaceConflictError("网站已在该 Space 中", code="member_exists")

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
        raise SpaceConflictError(
            "Space 成员关系已发生变化，请刷新后重试",
            code="member_conflict",
        ) from error

    member_count = await _member_count(session, user_id, space_id)
    return SpaceMemberAddResponse(
        space=_space_response(space, member_count),
        member=_member_response(member, site),
    )


async def _completed_member_batch(
    session: AsyncSession,
    user_id: str,
    space_id: str,
    site_ids: list[str],
    *,
    allow_empty: bool = False,
) -> SpaceMemberBatchResponse | None:
    """Return an idempotent result only for a fully reached outcome."""

    if not site_ids and not allow_empty:
        return None
    space = await session.scalar(
        select(Space)
        .where(
            Space.user_id == user_id,
            Space.id == space_id,
        )
        .with_for_update()
    )
    if space is None:
        return None
    existing_site_ids = set(
        (
            await session.scalars(
                select(SpaceMember.site_id)
                .where(
                    SpaceMember.user_id == user_id,
                    SpaceMember.space_id == space.id,
                    SpaceMember.site_id.in_(site_ids),
                )
                .with_for_update()
            )
        ).all()
    )
    if len(existing_site_ids) != len(site_ids):
        return None
    return SpaceMemberBatchResponse(
        space=_space_response(
            space,
            await _member_count(session, user_id, space.id),
        ),
        added_count=0,
        already_member_count=len(site_ids),
        site_ids=site_ids,
    )


def _space_batch_payload_hash(
    payload: SpaceMemberBatchRequest,
    normalized_name: str,
) -> str:
    target: dict[str, object] = {"mode": payload.target.mode}
    if payload.target.mode == "create":
        target["space_name"] = normalized_name
    else:
        target["space_id"] = payload.target.space_id
        target["expected_version"] = payload.target.expected_version
    encoded = json.dumps(
        {"target": target, "site_ids": payload.site_ids},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


async def _space_batch_receipt(
    session: AsyncSession,
    user_id: str,
    operation_id: str,
) -> SpaceBatchOperationReceipt | None:
    return await session.scalar(
        select(SpaceBatchOperationReceipt).where(
            SpaceBatchOperationReceipt.user_id == user_id,
            SpaceBatchOperationReceipt.operation_id == operation_id,
        )
    )


def _valid_space_batch_snapshot(
    response: SpaceMemberBatchResponse,
    *,
    target_space_id: str,
    site_ids: list[str],
) -> bool:
    return (
        response.site_ids == site_ids
        and len(set(response.site_ids)) == len(response.site_ids)
        and response.space.id == target_space_id
        and response.added_count >= 0
        and response.already_member_count >= 0
        and response.added_count + response.already_member_count == len(site_ids)
        and response.space.member_count >= len(site_ids)
        and response.space.version >= 1
    )


def _receipt_response(
    receipt: SpaceBatchOperationReceipt,
    *,
    payload_hash: str,
    target_mode: str,
    target_space_id: str,
    site_ids: list[str],
) -> SpaceMemberBatchResponse:
    if receipt.payload_hash != payload_hash:
        raise SpaceConflictError(
            "operation_id 已用于其他 Space 批量任务",
            code="idempotency_conflict",
        )
    try:
        response = SpaceMemberBatchResponse.model_validate_json(receipt.result_json)
        selected_site_ids = json.loads(receipt.selected_site_ids_json)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise SpaceConflictError(
            "Space 批量操作回执损坏",
            code="operation_receipt_invalid",
        ) from error
    if (
        not isinstance(selected_site_ids, list)
        or selected_site_ids != site_ids
        or receipt.target_mode != target_mode
        or receipt.target_space_id != target_space_id
        or receipt.result_space_id != target_space_id
        or receipt.added_count != response.added_count
        or receipt.already_member_count != response.already_member_count
        or not _valid_space_batch_snapshot(
            response,
            target_space_id=target_space_id,
            site_ids=site_ids,
        )
    ):
        raise SpaceConflictError(
            "Space 批量操作回执不一致",
            code="operation_receipt_invalid",
        )
    return response


async def _replayed_space_batch(
    session: AsyncSession,
    *,
    user_id: str,
    operation_id: str,
    payload_hash: str,
    target_mode: str,
    target_space_id: str,
    site_ids: list[str],
) -> SpaceMemberBatchResponse | None:
    receipt = await _space_batch_receipt(session, user_id, operation_id)
    if receipt is None:
        return None
    return _receipt_response(
        receipt,
        payload_hash=payload_hash,
        target_mode=target_mode,
        target_space_id=target_space_id,
        site_ids=site_ids,
    )


async def _batch_sites(
    session: AsyncSession,
    user_id: str,
    site_ids: list[str],
) -> dict[str, Site] | None:
    if not site_ids:
        return {}
    rows = list(
        (
            await session.scalars(
                select(Site)
                .where(
                    Site.user_id == user_id,
                    Site.id.in_(site_ids),
                )
                .with_for_update()
            )
        ).all()
    )
    sites = {site.id: site for site in rows}
    return sites if len(sites) == len(site_ids) else None


def _new_space_batch_receipt(
    *,
    user_id: str,
    operation_id: str,
    payload_hash: str,
    target_mode: str,
    target_space_id: str,
    response: SpaceMemberBatchResponse,
) -> SpaceBatchOperationReceipt:
    if target_mode not in {"create", "existing"} or not _valid_space_batch_snapshot(
        response,
        target_space_id=target_space_id,
        site_ids=response.site_ids,
    ):
        raise SpaceConflictError(
            "Space 批量操作结果不一致",
            code="operation_result_invalid",
        )
    return SpaceBatchOperationReceipt(
        user_id=user_id,
        operation_id=operation_id,
        payload_hash=payload_hash,
        target_mode=target_mode,
        target_space_id=target_space_id,
        selected_site_ids_json=json.dumps(response.site_ids, separators=(",", ":")),
        result_space_id=response.space.id,
        result_json=response.model_dump_json(),
        added_count=response.added_count,
        already_member_count=response.already_member_count,
    )


async def _commit_receipt_only(
    session: AsyncSession,
    receipt: SpaceBatchOperationReceipt,
    response: SpaceMemberBatchResponse,
) -> SpaceMemberBatchResponse:
    session.add(receipt)
    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        existing = await _space_batch_receipt(
            session,
            receipt.user_id,
            receipt.operation_id,
        )
        if existing is not None:
            return _receipt_response(
                existing,
                payload_hash=receipt.payload_hash,
                target_mode=receipt.target_mode,
                target_space_id=receipt.target_space_id,
                site_ids=response.site_ids,
            )
        raise SpaceConflictError(
            "Space 批量操作回执写入冲突",
            code="operation_receipt_conflict",
        ) from error
    return response


async def add_members_batch(
    session: AsyncSession,
    user_id: str,
    payload: SpaceMemberBatchRequest,
) -> SpaceMemberBatchResponse:
    """Create/use one Space and add all requested sites in one transaction.

    Both modes bind the normalized payload and immutable result to a durable
    operation receipt. Create additionally derives the Space id from the
    account and operation id, so an empty create is safe to replay too.
    """

    site_ids = list(payload.site_ids)
    operation_id = payload.operation_id.strip()
    space_name, normalized_name = _space_name(payload.target.space_name)
    payload_hash = _space_batch_payload_hash(payload, normalized_name)
    target_space_id = (
        space_batch_space_id(user_id, operation_id)
        if payload.target.mode == "create"
        else payload.target.space_id
    )
    assert target_space_id is not None

    replayed = await _replayed_space_batch(
        session,
        user_id=user_id,
        operation_id=operation_id,
        payload_hash=payload_hash,
        target_mode=payload.target.mode,
        target_space_id=target_space_id,
        site_ids=site_ids,
    )
    if replayed is not None:
        return replayed

    sites: dict[str, Site]

    async def load_sites_or_replay() -> SpaceMemberBatchResponse | None:
        nonlocal sites
        loaded_sites = await _batch_sites(session, user_id, site_ids)
        if loaded_sites is not None:
            sites = loaded_sites
            return None
        stored = await _replayed_space_batch(
            session,
            user_id=user_id,
            operation_id=operation_id,
            payload_hash=payload_hash,
            target_mode=payload.target.mode,
            target_space_id=target_space_id,
            site_ids=site_ids,
        )
        if stored is not None:
            return stored
        raise SpaceNotFoundError("网站不存在或不属于当前账号")

    if payload.target.mode == "create":
        operation_space_id = target_space_id
        operation_space = await session.scalar(
            select(Space)
            .where(
                Space.user_id == user_id,
                Space.id == operation_space_id,
            )
            .with_for_update()
        )
        if operation_space is not None:
            replayed = await _replayed_space_batch(
                session,
                user_id=user_id,
                operation_id=operation_id,
                payload_hash=payload_hash,
                target_mode="create",
                target_space_id=target_space_id,
                site_ids=site_ids,
            )
            if replayed is not None:
                return replayed
            replay = await _completed_member_batch(
                session,
                user_id,
                operation_space.id,
                site_ids,
                allow_empty=True,
            )
            if replay is not None:
                receipt = _new_space_batch_receipt(
                    user_id=user_id,
                    operation_id=operation_id,
                    payload_hash=payload_hash,
                    target_mode="create",
                    target_space_id=target_space_id,
                    response=replay,
                )
                return await _commit_receipt_only(session, receipt, replay)
            raise SpaceConflictError(
                "该创建任务的成员状态不完整",
                code="operation_conflict",
            )

        existing_space = await session.scalar(
            select(Space).where(
                Space.user_id == user_id,
                Space.normalized_name == normalized_name,
            )
        )
        if existing_space is not None:
            replayed = await _replayed_space_batch(
                session,
                user_id=user_id,
                operation_id=operation_id,
                payload_hash=payload_hash,
                target_mode="create",
                target_space_id=target_space_id,
                site_ids=site_ids,
            )
            if replayed is not None:
                return replayed
            raise SpaceConflictError("Space 名称已存在", code="duplicate_name")

        replayed = await load_sites_or_replay()
        if replayed is not None:
            return replayed

        space = Space(
            id=operation_space_id,
            user_id=user_id,
            name=space_name,
            normalized_name=normalized_name,
        )
        session.add(space)
        try:
            await session.flush()
            for position, site_id in enumerate(site_ids):
                session.add(
                    SpaceMember(
                        user_id=user_id,
                        space_id=space.id,
                        site_id=sites[site_id].id,
                        position=position,
                    )
                )
            response = SpaceMemberBatchResponse(
                space=_space_response(space, len(site_ids)),
                added_count=len(site_ids),
                already_member_count=0,
                site_ids=site_ids,
            )
            session.add(
                _new_space_batch_receipt(
                    user_id=user_id,
                    operation_id=operation_id,
                    payload_hash=payload_hash,
                    target_mode="create",
                    target_space_id=target_space_id,
                    response=response,
                )
            )
            await session.commit()
        except IntegrityError as error:
            await session.rollback()
            existing_receipt = await _space_batch_receipt(
                session,
                user_id,
                operation_id,
            )
            if existing_receipt is not None:
                return _receipt_response(
                    existing_receipt,
                    payload_hash=payload_hash,
                    target_mode="create",
                    target_space_id=target_space_id,
                    site_ids=site_ids,
                )
            replay = await _completed_member_batch(
                session,
                user_id,
                operation_space_id,
                site_ids,
                allow_empty=True,
            )
            if replay is not None:
                receipt = _new_space_batch_receipt(
                    user_id=user_id,
                    operation_id=operation_id,
                    payload_hash=payload_hash,
                    target_mode="create",
                    target_space_id=target_space_id,
                    response=replay,
                )
                return await _commit_receipt_only(session, receipt, replay)
            raise SpaceConflictError(
                "Space 或成员关系已发生变化，请刷新后重试",
                code="member_batch_conflict",
            ) from error
        return response

    assert payload.target.space_id is not None
    assert payload.target.expected_version is not None
    space = await session.scalar(
        select(Space)
        .where(
            Space.user_id == user_id,
            Space.id == payload.target.space_id,
        )
        .with_for_update()
    )
    if space is None:
        replayed = await _replayed_space_batch(
            session,
            user_id=user_id,
            operation_id=operation_id,
            payload_hash=payload_hash,
            target_mode="existing",
            target_space_id=target_space_id,
            site_ids=site_ids,
        )
        if replayed is not None:
            return replayed
        raise SpaceNotFoundError("Space 不存在")

    replayed = await _replayed_space_batch(
        session,
        user_id=user_id,
        operation_id=operation_id,
        payload_hash=payload_hash,
        target_mode="existing",
        target_space_id=target_space_id,
        site_ids=site_ids,
    )
    if replayed is not None:
        return replayed
    replayed = await load_sites_or_replay()
    if replayed is not None:
        return replayed

    existing_site_ids = set(
        (
            await session.scalars(
                select(SpaceMember.site_id).where(
                    SpaceMember.user_id == user_id,
                    SpaceMember.space_id == space.id,
                    SpaceMember.site_id.in_(site_ids),
                )
            )
        ).all()
    )
    missing_site_ids = [site_id for site_id in site_ids if site_id not in existing_site_ids]
    if not missing_site_ids:
        response = SpaceMemberBatchResponse(
            space=_space_response(
                space,
                await _member_count(session, user_id, space.id),
            ),
            added_count=0,
            already_member_count=len(existing_site_ids),
            site_ids=site_ids,
        )
        receipt = _new_space_batch_receipt(
            user_id=user_id,
            operation_id=operation_id,
            payload_hash=payload_hash,
            target_mode="existing",
            target_space_id=target_space_id,
            response=response,
        )
        return await _commit_receipt_only(session, receipt, response)
    if space.version != payload.target.expected_version:
        await session.rollback()
        existing_receipt = await _space_batch_receipt(
            session,
            user_id,
            operation_id,
        )
        if existing_receipt is not None:
            return _receipt_response(
                existing_receipt,
                payload_hash=payload_hash,
                target_mode="existing",
                target_space_id=target_space_id,
                site_ids=site_ids,
            )
        replay = await _completed_member_batch(
            session,
            user_id,
            payload.target.space_id,
            site_ids,
        )
        if replay is not None:
            receipt = _new_space_batch_receipt(
                user_id=user_id,
                operation_id=operation_id,
                payload_hash=payload_hash,
                target_mode="existing",
                target_space_id=target_space_id,
                response=replay,
            )
            return await _commit_receipt_only(session, receipt, replay)
        raise SpaceConflictError(
            "Space 已被修改，请刷新草稿后重试",
            code="version_conflict",
        )

    current_member_count = await _member_count(session, user_id, space.id)
    next_position = int(
        await session.scalar(
            select(func.coalesce(func.max(SpaceMember.position), -1) + 1).where(
                SpaceMember.user_id == user_id,
                SpaceMember.space_id == space.id,
            )
        )
        or 0
    )
    space_id = space.id
    try:
        await _claim_version(session, space, payload.target.expected_version)
        for offset, site_id in enumerate(missing_site_ids):
            session.add(
                SpaceMember(
                    user_id=user_id,
                    space_id=space.id,
                    site_id=sites[site_id].id,
                    position=next_position + offset,
                )
            )
        response = SpaceMemberBatchResponse(
            space=_space_response(
                space,
                current_member_count + len(missing_site_ids),
            ),
            added_count=len(missing_site_ids),
            already_member_count=len(existing_site_ids),
            site_ids=site_ids,
        )
        session.add(
            _new_space_batch_receipt(
                user_id=user_id,
                operation_id=operation_id,
                payload_hash=payload_hash,
                target_mode="existing",
                target_space_id=target_space_id,
                response=response,
            )
        )
        await session.commit()
    except SpaceConflictError:
        await session.rollback()
        existing_receipt = await _space_batch_receipt(
            session,
            user_id,
            operation_id,
        )
        if existing_receipt is not None:
            return _receipt_response(
                existing_receipt,
                payload_hash=payload_hash,
                target_mode="existing",
                target_space_id=target_space_id,
                site_ids=site_ids,
            )
        replay = await _completed_member_batch(
            session,
            user_id,
            space_id,
            site_ids,
        )
        if replay is not None:
            receipt = _new_space_batch_receipt(
                user_id=user_id,
                operation_id=operation_id,
                payload_hash=payload_hash,
                target_mode="existing",
                target_space_id=target_space_id,
                response=replay,
            )
            return await _commit_receipt_only(session, receipt, replay)
        raise
    except IntegrityError as error:
        await session.rollback()
        existing_receipt = await _space_batch_receipt(
            session,
            user_id,
            operation_id,
        )
        if existing_receipt is not None:
            return _receipt_response(
                existing_receipt,
                payload_hash=payload_hash,
                target_mode="existing",
                target_space_id=target_space_id,
                site_ids=site_ids,
            )
        replay = await _completed_member_batch(
            session,
            user_id,
            space_id,
            site_ids,
        )
        if replay is not None:
            receipt = _new_space_batch_receipt(
                user_id=user_id,
                operation_id=operation_id,
                payload_hash=payload_hash,
                target_mode="existing",
                target_space_id=target_space_id,
                response=replay,
            )
            return await _commit_receipt_only(session, receipt, replay)
        raise SpaceConflictError(
            "Space 成员关系已发生变化，请刷新草稿后重试",
            code="member_batch_conflict",
        ) from error

    return response


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
        raise SpaceConflictError(
            "Space 已被修改，请刷新后重试",
            code="version_conflict",
        )

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
        raise SpaceNotFoundError("Space 成员不存在", code="member_not_found")
    if payload.before_site_id is not None and payload.before_site_id not in current_set:
        raise SpaceNotFoundError("排序定位成员不存在", code="member_not_found")

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
        raise SpaceConflictError(
            "Space 成员顺序已发生变化，请刷新后重试",
            code="member_order_conflict",
        ) from error
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
        raise SpaceConflictError(
            "Space 已被修改，请刷新后重试",
            code="version_conflict",
        )
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
        raise SpaceConflictError(
            "Space 已被修改，请刷新后重试",
            code="version_conflict",
        )
    await session.commit()
    return SpaceDeleteResponse(
        message="Space 已删除，网站仍保留在网址库中",
        space_id=space_id,
        unlinked_site_count=member_count,
    )


async def resolve_space_reference(
    session: AsyncSession,
    user_id: str,
    reference: str,
) -> Space | None:
    """Find one of the account's Spaces by id or by exact name.

    The Agent talks in names ("设计"), never ids, so a lookup by name is what
    makes "把 Figma 移到设计" resolvable at all.  The name is normalised exactly
    the way ``_space_name`` normalises it on write (NFKC + whitespace collapse +
    casefold), otherwise a Space created through the UI would be unfindable
    here.  The query is account-scoped like every other read in this module.
    """

    display = " ".join(unicodedata.normalize("NFKC", reference).split())
    if not display:
        return None
    by_id = await session.scalar(select(Space).where(Space.user_id == user_id, Space.id == display))
    if by_id is not None:
        return by_id
    return await session.scalar(
        select(Space).where(
            Space.user_id == user_id,
            Space.normalized_name == display.casefold(),
        )
    )


async def is_member(
    session: AsyncSession,
    user_id: str,
    space_id: str,
    site_id: str,
) -> bool:
    """Whether a site currently belongs to a Space, within this account."""

    return (
        await session.scalar(
            select(SpaceMember.site_id).where(
                SpaceMember.user_id == user_id,
                SpaceMember.space_id == space_id,
                SpaceMember.site_id == site_id,
            )
        )
    ) is not None
