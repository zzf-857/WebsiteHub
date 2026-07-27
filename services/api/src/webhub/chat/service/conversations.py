from __future__ import annotations

from datetime import datetime
from typing import Literal

from sqlalchemy import and_, delete, desc, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.db.models import utc_now

from ..models import (
    Conversation,
)
from ..schemas import (
    ConversationDeleteResponse,
    ConversationHistoryResponse,
    ConversationResponse,
    HistoryGroupResponse,
)
from ._common import (
    ChatConflictError,
    ChatValidationError,
    _as_utc,
    _conversation_response,
    _decode_cursor,
    _encode_cursor,
    _group_for,
    _owned_conversation,
    _title,
)

SortDirection = Literal["asc", "desc"]
MessageRole = Literal["system", "user", "assistant", "tool"]
MessageStatus = Literal["streaming", "complete", "error", "aborted"]

MAX_HISTORY_LIMIT = 100
MAX_MESSAGE_LIMIT = 100
MAX_IDEMPOTENCY_KEY_LENGTH = 200
_MAX_CAS_RETRIES = 3


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
