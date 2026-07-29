from __future__ import annotations

from typing import Any, Literal

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.db.models import utc_now

from ..models import (
    MAX_MESSAGE_CONTENT_LENGTH,
    MAX_MESSAGE_JSON_BYTES,
    Conversation,
    ConversationMessage,
)
from ..schemas import (
    ConversationDetailResponse,
    ConversationMessageResponse,
    MessageListResponse,
)
from ._common import (
    ChatConflictError,
    ChatValidationError,
    MessageAppendResult,
    _conversation_response,
    _decode_cursor,
    _derived_title,
    _encode_cursor,
    _idempotency_hash,
    _json_text,
    _json_value,
    _message_response,
    _owned_conversation,
    _owned_message,
    _payload_hash,
    _server_owned_metadata,
)

SortDirection = Literal["asc", "desc"]
MessageRole = Literal["system", "user", "assistant", "tool"]
MessageStatus = Literal["streaming", "complete", "error", "aborted"]

MAX_HISTORY_LIMIT = 100
MAX_MESSAGE_LIMIT = 100
MAX_IDEMPOTENCY_KEY_LENGTH = 200
_MAX_CAS_RETRIES = 3


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


async def list_recent_messages(
    session: AsyncSession,
    user_id: str,
    conversation_id: str,
    *,
    limit: int = 20,
) -> list[ConversationMessageResponse]:
    """Return the newest ``limit`` messages, oldest first.

    ``list_messages`` paginates forward from the start of a conversation, which
    is right for reading a transcript but wrong for building a prompt: taking
    its first page would feed the model the *oldest* turns and silently drop
    everything recent once a conversation outgrows the window.  This query
    walks backwards instead and then restores chronological order.
    """

    await _owned_conversation(session, user_id, conversation_id)
    if limit < 1 or limit > MAX_MESSAGE_LIMIT:
        raise ChatValidationError(f"每页消息数必须在 1 到 {MAX_MESSAGE_LIMIT} 之间")
    statement = (
        select(ConversationMessage)
        .where(
            ConversationMessage.user_id == user_id,
            ConversationMessage.conversation_id == conversation_id,
        )
        .order_by(
            ConversationMessage.sequence.desc(),
            ConversationMessage.id.desc(),
        )
        .limit(limit)
    )
    rows = list((await session.scalars(statement)).all())
    rows.reverse()
    return [_message_response(message) for message in rows]


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
    commit: bool = True,
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
            if commit:
                await session.commit()
            else:
                await session.flush()
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
    expected_status: MessageStatus | None = None,
    commit: bool = True,
) -> ConversationMessageResponse:
    message = await _owned_message(session, user_id, conversation_id, message_id)
    if message.version != expected_version:
        raise ChatConflictError("消息已被修改，请刷新后重试", code="version_conflict")
    if expected_status is not None and message.status != expected_status:
        raise ChatConflictError("消息状态已终止，请刷新后重试", code="status_conflict")
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
            *(
                (ConversationMessage.status == expected_status,)
                if expected_status is not None
                else ()
            ),
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
    if commit:
        await session.commit()
    else:
        await session.flush()
    await session.refresh(message)
    return _message_response(message)


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
