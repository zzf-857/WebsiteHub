from __future__ import annotations

import base64
import binascii
import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import and_, delete, desc, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.db.models import utc_now

from .commands import default_slash_command_registry, parse_slash_command
from .models import (
    MAX_CONVERSATION_TITLE_LENGTH,
    MAX_MESSAGE_CONTENT_LENGTH,
    MAX_MESSAGE_JSON_BYTES,
    Conversation,
    ConversationMessage,
)
from .schemas import (
    ConversationDeleteResponse,
    ConversationDetailResponse,
    ConversationHistoryResponse,
    ConversationMessageResponse,
    ConversationResponse,
    HistoryGroupResponse,
    MessageListResponse,
)

SortDirection = Literal["asc", "desc"]
MessageRole = Literal["system", "user", "assistant", "tool"]
MessageStatus = Literal["streaming", "complete", "error", "aborted"]

MAX_HISTORY_LIMIT = 100
MAX_MESSAGE_LIMIT = 100
MAX_IDEMPOTENCY_KEY_LENGTH = 200
_MAX_CAS_RETRIES = 3


class ChatError(Exception):
    status_code = 400
    code = "chat_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        self.message = message
        self.code = code or type(self).code
        super().__init__(message)


class ChatNotFoundError(ChatError):
    status_code = 404
    code = "not_found"


class ChatConflictError(ChatError):
    status_code = 409
    code = "conflict"


class ChatValidationError(ChatError):
    status_code = 422
    code = "validation_error"


@dataclass(frozen=True, slots=True)
class MessageAppendResult:
    conversation: ConversationResponse
    message: ConversationMessageResponse
    replayed: bool


def _normalized_text(value: str, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ChatValidationError(f"{field}必须是文本")
    normalized = " ".join(unicodedata.normalize("NFKC", value).split())
    if not normalized:
        raise ChatValidationError(f"{field}不能为空")
    if len(normalized) > maximum:
        raise ChatValidationError(f"{field}不能超过 {maximum} 个字符")
    return normalized


def _title(value: str | None) -> str:
    if value is None:
        return "新会话"
    return _normalized_text(
        value,
        field="会话标题",
        maximum=MAX_CONVERSATION_TITLE_LENGTH,
    )


def _derived_title(content: str) -> str:
    compact = " ".join(unicodedata.normalize("NFKC", content).split())
    if not compact:
        return "新会话"
    maximum = 60
    if len(compact) <= maximum:
        return compact
    return compact[: maximum - 1].rstrip() + "…"


def _json_text(value: object, *, field: str, expected: type, maximum: int) -> str:
    if not isinstance(value, expected):
        expected_name = "数组" if expected is list else "对象"
        raise ChatValidationError(f"{field}必须是{expected_name}")
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise ChatValidationError(f"{field}包含不可序列化的数据") from error
    if len(encoded.encode("utf-8")) > maximum:
        raise ChatValidationError(f"{field}过大，不能超过 {maximum} 字节")
    return encoded


def _json_value(value: str, *, field: str, expected: type) -> object:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ChatError(f"{field}数据损坏", code="stored_data_invalid") from error
    if not isinstance(parsed, expected):
        raise ChatError(f"{field}数据损坏", code="stored_data_invalid")
    return parsed


def _idempotency_hash(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        raise ChatValidationError("幂等键不能为空")
    if len(normalized) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise ChatValidationError(f"幂等键不能超过 {MAX_IDEMPOTENCY_KEY_LENGTH} 个字符")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _payload_hash(
    *,
    role: str,
    content: str,
    status: str,
    parts_json: str,
    sources_json: str,
    artifacts_json: str,
    metadata_json: str,
) -> str:
    payload = "\x1f".join(
        (role, content, status, parts_json, sources_json, artifacts_json, metadata_json)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _server_owned_metadata(*, role: str, content: str, metadata_json: str) -> str:
    metadata = json.loads(metadata_json)
    metadata.pop("slash_command", None)
    if role == "user":
        invocation = parse_slash_command(
            content,
            registry=default_slash_command_registry(),
        )
        if invocation.is_command:
            metadata["slash_command"] = invocation.metadata()
    return _json_text(
        metadata,
        field="metadata",
        expected=dict,
        maximum=MAX_MESSAGE_JSON_BYTES,
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _conversation_response(conversation: Conversation) -> ConversationResponse:
    return ConversationResponse(
        id=conversation.id,
        title=conversation.title or "新会话",
        title_is_custom=conversation.title_is_custom,
        version=conversation.version,
        message_count=conversation.message_count,
        last_message_at=_as_utc(conversation.last_message_at),
        created_at=_as_utc(conversation.created_at),
        updated_at=_as_utc(conversation.updated_at),
    )


def _message_response(message: ConversationMessage) -> ConversationMessageResponse:
    return ConversationMessageResponse(
        id=message.id,
        conversation_id=message.conversation_id,
        sequence=message.sequence,
        role=message.role,  # type: ignore[arg-type]
        content=message.content,
        parts=_json_value(message.parts_json, field="parts", expected=list),  # type: ignore[arg-type]
        sources=_json_value(message.sources_json, field="sources", expected=list),  # type: ignore[arg-type]
        artifacts=_json_value(message.artifacts_json, field="artifacts", expected=list),  # type: ignore[arg-type]
        metadata=_json_value(message.metadata_json, field="metadata", expected=dict),  # type: ignore[arg-type]
        status=message.status,  # type: ignore[arg-type]
        version=message.version,
        created_at=_as_utc(message.created_at),
        updated_at=_as_utc(message.updated_at),
    )


async def _owned_conversation(
    session: AsyncSession,
    user_id: str,
    conversation_id: str,
) -> Conversation:
    conversation = await session.scalar(
        select(Conversation).where(
            Conversation.user_id == user_id,
            Conversation.id == conversation_id,
        )
    )
    if conversation is None:
        raise ChatNotFoundError("会话不存在")
    return conversation


async def _owned_message(
    session: AsyncSession,
    user_id: str,
    conversation_id: str,
    message_id: str,
) -> ConversationMessage:
    message = await session.scalar(
        select(ConversationMessage).where(
            ConversationMessage.user_id == user_id,
            ConversationMessage.conversation_id == conversation_id,
            ConversationMessage.id == message_id,
        )
    )
    if message is None:
        raise ChatNotFoundError("消息不存在")
    return message


def _cursor_scope(*, kind: str, user_id: str, resource_id: str | None, limit: int) -> str:
    raw = f"{kind}:{user_id}:{resource_id or ''}:{limit}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _encode_cursor(
    *,
    kind: str,
    user_id: str,
    resource_id: str | None,
    limit: int,
    value: str,
    item_id: str,
) -> str:
    payload = {
        "v": 1,
        "kind": kind,
        "scope": _cursor_scope(
            kind=kind,
            user_id=user_id,
            resource_id=resource_id,
            limit=limit,
        ),
        "value": value,
        "id": item_id,
    }
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def _decode_cursor(
    cursor: str,
    *,
    kind: str,
    user_id: str,
    resource_id: str | None,
    limit: int,
) -> tuple[str, str]:
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(cursor + padding))
        expected_scope = _cursor_scope(
            kind=kind,
            user_id=user_id,
            resource_id=resource_id,
            limit=limit,
        )
        if (
            not isinstance(payload, dict)
            or payload.get("v") != 1
            or payload.get("kind") != kind
            or payload.get("scope") != expected_scope
            or not isinstance(payload.get("value"), str)
            or not isinstance(payload.get("id"), str)
        ):
            raise ValueError
        return payload["value"], payload["id"]
    except (
        binascii.Error,
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ) as error:
        raise ChatValidationError("分页游标无效") from error


def _group_for(
    value: datetime,
    *,
    now: datetime,
    timezone_offset_minutes: int,
) -> tuple[str, str, str | None]:
    offset = timedelta(minutes=timezone_offset_minutes)
    current_date = (_as_utc(now) + offset).date()
    target_date = (_as_utc(value) + offset).date()
    age = max((current_date - target_date).days, 0)
    if age == 0:
        return "today", "今天", None
    if age == 1:
        return "yesterday", "昨天", None
    if age <= 7:
        return "last_7_days", "近 7 天", None
    if age <= 30:
        return "last_30_days", "近 30 天", None
    month = target_date.strftime("%Y-%m")
    return f"month:{month}", f"{target_date.year}年{target_date.month}月", month


async def create_conversation(
    session: AsyncSession,
    user_id: str,
    *,
    title: str | None = None,
) -> ConversationResponse:
    now = utc_now()
    conversation = Conversation(
        user_id=user_id,
        title=_title(title),
        title_is_custom=title is not None,
        version=1,
        message_count=0,
        last_message_at=now,
        created_at=now,
        updated_at=now,
    )
    session.add(conversation)
    await session.commit()
    return _conversation_response(conversation)


async def get_conversation(
    session: AsyncSession,
    user_id: str,
    conversation_id: str,
) -> ConversationResponse:
    return _conversation_response(await _owned_conversation(session, user_id, conversation_id))


async def list_conversations(
    session: AsyncSession,
    user_id: str,
    *,
    cursor: str | None = None,
    limit: int = 50,
    now: datetime | None = None,
    timezone_offset_minutes: int = 480,
) -> ConversationHistoryResponse:
    if limit < 1 or limit > MAX_HISTORY_LIMIT:
        raise ChatValidationError(f"每页会话数必须在 1 到 {MAX_HISTORY_LIMIT} 之间")
    if timezone_offset_minutes < -840 or timezone_offset_minutes > 840:
        raise ChatValidationError("时区偏移必须在 -840 到 840 分钟之间")
    current_time = _as_utc(now or utc_now())
    statement = select(Conversation).where(Conversation.user_id == user_id)
    if cursor:
        value, item_id = _decode_cursor(
            cursor,
            kind="conversations",
            user_id=user_id,
            resource_id=f"timezone:{timezone_offset_minutes}",
            limit=limit,
        )
        try:
            cursor_time = datetime.fromisoformat(value)
        except ValueError as error:
            raise ChatValidationError("分页游标无效") from error
        cursor_time = _as_utc(cursor_time)
        statement = statement.where(
            or_(
                Conversation.last_message_at < cursor_time,
                and_(
                    Conversation.last_message_at == cursor_time,
                    Conversation.id < item_id,
                ),
            )
        )
    statement = statement.order_by(
        desc(Conversation.last_message_at),
        desc(Conversation.id),
    ).limit(limit + 1)
    rows = list((await session.scalars(statement)).all())
    has_next = len(rows) > limit
    rows = rows[:limit]
    groups: list[HistoryGroupResponse] = []
    group_by_key: dict[str, HistoryGroupResponse] = {}
    for conversation in rows:
        key, label, month = _group_for(
            conversation.last_message_at,
            now=current_time,
            timezone_offset_minutes=timezone_offset_minutes,
        )
        group = group_by_key.get(key)
        if group is None:
            group = HistoryGroupResponse(key=key, label=label, month=month, items=[])
            group_by_key[key] = group
            groups.append(group)
        group.items.append(_conversation_response(conversation))
    next_cursor = None
    if has_next and rows:
        last = rows[-1]
        next_cursor = _encode_cursor(
            kind="conversations",
            user_id=user_id,
            resource_id=f"timezone:{timezone_offset_minutes}",
            limit=limit,
            value=_as_utc(last.last_message_at).isoformat(),
            item_id=last.id,
        )
    total_count = int(
        await session.scalar(
            select(func.count(Conversation.id)).where(Conversation.user_id == user_id)
        )
        or 0
    )
    return ConversationHistoryResponse(
        groups=groups,
        next_cursor=next_cursor,
        total_count=total_count,
    )


async def rename_conversation(
    session: AsyncSession,
    user_id: str,
    conversation_id: str,
    *,
    title: str,
    expected_version: int,
) -> ConversationResponse:
    normalized_title = _title(title)
    conversation = await _owned_conversation(session, user_id, conversation_id)
    if conversation.version != expected_version:
        raise ChatConflictError("会话已被修改，请刷新后重试", code="version_conflict")
    result = await session.execute(
        update(Conversation)
        .where(
            Conversation.user_id == user_id,
            Conversation.id == conversation_id,
            Conversation.version == expected_version,
        )
        .values(
            title=normalized_title,
            title_is_custom=True,
            version=Conversation.version + 1,
            updated_at=utc_now(),
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        await session.rollback()
        raise ChatConflictError("会话已被修改，请刷新后重试", code="version_conflict")
    await session.commit()
    await session.refresh(conversation)
    return _conversation_response(conversation)


async def delete_conversation(
    session: AsyncSession,
    user_id: str,
    conversation_id: str,
    *,
    expected_version: int,
) -> ConversationDeleteResponse:
    conversation = await _owned_conversation(session, user_id, conversation_id)
    if conversation.version != expected_version:
        raise ChatConflictError("会话已被修改，请刷新后重试", code="version_conflict")
    statement = delete(Conversation).where(
        Conversation.user_id == user_id,
        Conversation.id == conversation_id,
        Conversation.version == expected_version,
    )
    result = await session.execute(statement)
    if result.rowcount != 1:  # type: ignore[attr-defined]
        await session.rollback()
        raise ChatConflictError("会话已被修改，请刷新后重试", code="version_conflict")
    await session.commit()
    return ConversationDeleteResponse(message="会话已删除", conversation_id=conversation_id)


async def list_messages(
    session: AsyncSession,
    user_id: str,
    conversation_id: str,
    *,
    cursor: str | None = None,
    limit: int = 100,
) -> MessageListResponse:
    await _owned_conversation(session, user_id, conversation_id)
    if limit < 1 or limit > MAX_MESSAGE_LIMIT:
        raise ChatValidationError(f"每页消息数必须在 1 到 {MAX_MESSAGE_LIMIT} 之间")
    statement = select(ConversationMessage).where(
        ConversationMessage.user_id == user_id,
        ConversationMessage.conversation_id == conversation_id,
    )
    if cursor:
        value, item_id = _decode_cursor(
            cursor,
            kind="messages",
            user_id=user_id,
            resource_id=conversation_id,
            limit=limit,
        )
        try:
            sequence = int(value)
        except ValueError as error:
            raise ChatValidationError("分页游标无效") from error
        statement = statement.where(
            or_(
                ConversationMessage.sequence > sequence,
                and_(
                    ConversationMessage.sequence == sequence,
                    ConversationMessage.id > item_id,
                ),
            )
        )
    statement = statement.order_by(
        ConversationMessage.sequence.asc(),
        ConversationMessage.id.asc(),
    ).limit(limit + 1)
    rows = list((await session.scalars(statement)).all())
    has_next = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if has_next and rows:
        last = rows[-1]
        next_cursor = _encode_cursor(
            kind="messages",
            user_id=user_id,
            resource_id=conversation_id,
            limit=limit,
            value=str(last.sequence),
            item_id=last.id,
        )
    return MessageListResponse(
        items=[_message_response(message) for message in rows],
        next_cursor=next_cursor,
    )


async def get_conversation_detail(
    session: AsyncSession,
    user_id: str,
    conversation_id: str,
    *,
    cursor: str | None = None,
    limit: int = 100,
) -> ConversationDetailResponse:
    conversation = await _owned_conversation(session, user_id, conversation_id)
    messages = await list_messages(
        session,
        user_id,
        conversation_id,
        cursor=cursor,
        limit=limit,
    )
    return ConversationDetailResponse(
        conversation=_conversation_response(conversation),
        messages=messages.items,
        next_cursor=messages.next_cursor,
    )


def _message_payload(
    *,
    role: str,
    content: str,
    parts: list[Any] | None,
    sources: list[Any] | None,
    artifacts: list[Any] | None,
    metadata: dict[str, Any] | None,
    status: str,
) -> tuple[str, str, str, str, str]:
    if role not in {"system", "user", "assistant", "tool"}:
        raise ChatValidationError("消息角色无效")
    if status not in {"streaming", "complete", "error", "aborted"}:
        raise ChatValidationError("消息状态无效")
    if not isinstance(content, str):
        raise ChatValidationError("消息内容必须是文本")
    if len(content) > MAX_MESSAGE_CONTENT_LENGTH:
        raise ChatValidationError(f"消息内容不能超过 {MAX_MESSAGE_CONTENT_LENGTH} 个字符")
    if role == "user" and not content.strip():
        raise ChatValidationError("用户消息不能为空")
    parts_json = _json_text(
        [] if parts is None else parts,
        field="parts",
        expected=list,
        maximum=MAX_MESSAGE_JSON_BYTES,
    )
    sources_json = _json_text(
        [] if sources is None else sources,
        field="sources",
        expected=list,
        maximum=MAX_MESSAGE_JSON_BYTES,
    )
    artifacts_json = _json_text(
        [] if artifacts is None else artifacts,
        field="artifacts",
        expected=list,
        maximum=MAX_MESSAGE_JSON_BYTES,
    )
    metadata_json = _json_text(
        {} if metadata is None else metadata,
        field="metadata",
        expected=dict,
        maximum=MAX_MESSAGE_JSON_BYTES,
    )
    total_bytes = sum(
        len(value.encode("utf-8"))
        for value in (content, parts_json, sources_json, artifacts_json, metadata_json)
    )
    if total_bytes > MAX_MESSAGE_JSON_BYTES * 2:
        raise ChatValidationError("消息负载过大")
    return content, parts_json, sources_json, artifacts_json, metadata_json


async def _existing_idempotent_message(
    session: AsyncSession,
    *,
    user_id: str,
    conversation_id: str,
    idempotency_key_hash: str | None,
) -> ConversationMessage | None:
    if idempotency_key_hash is None:
        return None
    return await session.scalar(
        select(ConversationMessage).where(
            ConversationMessage.user_id == user_id,
            ConversationMessage.conversation_id == conversation_id,
            ConversationMessage.idempotency_key_hash == idempotency_key_hash,
        )
    )


async def append_message(
    session: AsyncSession,
    user_id: str,
    conversation_id: str,
    *,
    role: MessageRole,
    content: str,
    parts: list[Any] | None = None,
    sources: list[Any] | None = None,
    artifacts: list[Any] | None = None,
    metadata: dict[str, Any] | None = None,
    status: MessageStatus = "complete",
    idempotency_key: str | None = None,
    expected_version: int | None = None,
) -> MessageAppendResult:
    content, parts_json, sources_json, artifacts_json, metadata_json = _message_payload(
        role=role,
        content=content,
        parts=parts,
        sources=sources,
        artifacts=artifacts,
        metadata=metadata,
        status=status,
    )
    idempotency_key_hash = _idempotency_hash(idempotency_key)
    metadata_json = _server_owned_metadata(
        role=role,
        content=content,
        metadata_json=metadata_json,
    )
    payload_hash = _payload_hash(
        role=role,
        content=content,
        status=status,
        parts_json=parts_json,
        sources_json=sources_json,
        artifacts_json=artifacts_json,
        metadata_json=metadata_json,
    )

    for attempt in range(_MAX_CAS_RETRIES):
        conversation = await _owned_conversation(session, user_id, conversation_id)
        existing = await _existing_idempotent_message(
            session,
            user_id=user_id,
            conversation_id=conversation_id,
            idempotency_key_hash=idempotency_key_hash,
        )
        if existing is not None:
            if existing.payload_hash != payload_hash:
                raise ChatConflictError(
                    "幂等键已用于其他消息",
                    code="idempotency_conflict",
                )
            return MessageAppendResult(
                conversation=_conversation_response(conversation),
                message=_message_response(existing),
                replayed=True,
            )
        if expected_version is not None and conversation.version != expected_version:
            raise ChatConflictError("会话已被修改，请刷新后重试", code="version_conflict")

        now = utc_now()
        next_sequence = conversation.message_count + 1
        title = conversation.title
        if role == "user" and not conversation.title_is_custom and conversation.message_count == 0:
            title = _derived_title(content)
        result = await session.execute(
            update(Conversation)
            .where(
                Conversation.user_id == user_id,
                Conversation.id == conversation_id,
                Conversation.version == conversation.version,
                Conversation.message_count == conversation.message_count,
            )
            .values(
                title=title,
                version=Conversation.version + 1,
                message_count=Conversation.message_count + 1,
                last_message_at=now,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:  # type: ignore[attr-defined]
            await session.rollback()
            if expected_version is not None:
                raise ChatConflictError("会话已被修改，请刷新后重试", code="version_conflict")
            continue
        message = ConversationMessage(
            user_id=user_id,
            conversation_id=conversation_id,
            sequence=next_sequence,
            role=role,
            content=content,
            parts_json=parts_json,
            sources_json=sources_json,
            artifacts_json=artifacts_json,
            metadata_json=metadata_json,
            status=status,
            version=1,
            idempotency_key_hash=idempotency_key_hash,
            payload_hash=payload_hash,
            created_at=now,
            updated_at=now,
        )
        session.add(message)
        try:
            await session.commit()
        except IntegrityError as error:
            await session.rollback()
            existing = await _existing_idempotent_message(
                session,
                user_id=user_id,
                conversation_id=conversation_id,
                idempotency_key_hash=idempotency_key_hash,
            )
            if existing is not None:
                if existing.payload_hash != payload_hash:
                    raise ChatConflictError(
                        "幂等键已用于其他消息",
                        code="idempotency_conflict",
                    ) from error
                refreshed = await _owned_conversation(session, user_id, conversation_id)
                return MessageAppendResult(
                    conversation=_conversation_response(refreshed),
                    message=_message_response(existing),
                    replayed=True,
                )
            if attempt + 1 >= _MAX_CAS_RETRIES:
                raise ChatConflictError(
                    "消息写入冲突，请重试",
                    code="write_conflict",
                ) from error
            continue
        await session.refresh(conversation)
        await session.refresh(message)
        return MessageAppendResult(
            conversation=_conversation_response(conversation),
            message=_message_response(message),
            replayed=False,
        )
    raise ChatConflictError("消息写入冲突，请重试", code="write_conflict")


async def update_message(
    session: AsyncSession,
    user_id: str,
    conversation_id: str,
    message_id: str,
    *,
    expected_version: int,
    content: str | None = None,
    parts: list[Any] | None = None,
    sources: list[Any] | None = None,
    artifacts: list[Any] | None = None,
    metadata: dict[str, Any] | None = None,
    status: MessageStatus | None = None,
) -> ConversationMessageResponse:
    message = await _owned_message(session, user_id, conversation_id, message_id)
    if message.version != expected_version:
        raise ChatConflictError("消息已被修改，请刷新后重试", code="version_conflict")
    next_content = message.content if content is None else content
    next_parts = (
        _json_value(message.parts_json, field="parts", expected=list) if parts is None else parts
    )
    next_sources = (
        _json_value(message.sources_json, field="sources", expected=list)
        if sources is None
        else sources
    )
    next_artifacts = (
        _json_value(message.artifacts_json, field="artifacts", expected=list)
        if artifacts is None
        else artifacts
    )
    next_metadata = (
        _json_value(message.metadata_json, field="metadata", expected=dict)
        if metadata is None
        else metadata
    )
    next_status = message.status if status is None else status
    _, parts_json, sources_json, artifacts_json, metadata_json = _message_payload(
        role=message.role,
        content=next_content,
        parts=next_parts,  # type: ignore[arg-type]
        sources=next_sources,  # type: ignore[arg-type]
        artifacts=next_artifacts,  # type: ignore[arg-type]
        metadata=next_metadata,  # type: ignore[arg-type]
        status=next_status,
    )
    metadata_json = _server_owned_metadata(
        role=message.role,
        content=next_content,
        metadata_json=metadata_json,
    )
    now = utc_now()
    result = await session.execute(
        update(ConversationMessage)
        .where(
            ConversationMessage.user_id == user_id,
            ConversationMessage.conversation_id == conversation_id,
            ConversationMessage.id == message_id,
            ConversationMessage.version == expected_version,
        )
        .values(
            content=next_content,
            parts_json=parts_json,
            sources_json=sources_json,
            artifacts_json=artifacts_json,
            metadata_json=metadata_json,
            status=next_status,
            version=ConversationMessage.version + 1,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        await session.rollback()
        raise ChatConflictError("消息已被修改，请刷新后重试", code="version_conflict")
    await session.execute(
        update(Conversation)
        .where(
            Conversation.user_id == user_id,
            Conversation.id == conversation_id,
        )
        .values(last_message_at=now, updated_at=now)
    )
    await session.commit()
    await session.refresh(message)
    return _message_response(message)


def slash_command_definitions() -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "name": definition.name,
            "aliases": list(definition.aliases),
            "description": definition.description,
            "usage": definition.usage,
            "argument_hint": definition.argument_hint,
        }
        for definition in default_slash_command_registry().list()
    )
