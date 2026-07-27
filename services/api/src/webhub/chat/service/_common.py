from __future__ import annotations

import base64
import binascii
import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..commands import default_slash_command_registry, parse_slash_command
from ..models import (
    MAX_CONVERSATION_TITLE_LENGTH,
    MAX_MESSAGE_JSON_BYTES,
    Conversation,
    ConversationMessage,
)
from ..schemas import (
    ConversationMessageResponse,
    ConversationResponse,
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
