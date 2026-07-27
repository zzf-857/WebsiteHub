"""会话与消息服务层。

原本是单文件 1033 行。按关注点拆开后本模块只做门面，调用方沿用
``from webhub.chat import service`` 再用 ``service.X``，签名与行为不变。

- ``_common``        异常、校验、摘要、游标、响应构造、归属查询
- ``conversations``  会话的增删改查与详情
- ``messages``       消息列表、追加、更新（含幂等重放）
- ``commands``       斜杠命令定义与草稿确认落库
"""

from __future__ import annotations

from ._common import (
    ChatConflictError,
    ChatError,
    ChatNotFoundError,
    ChatValidationError,
    MessageAppendResult,
    _as_utc,
    _conversation_response,
    _cursor_scope,
    _decode_cursor,
    _derived_title,
    _encode_cursor,
    _group_for,
    _idempotency_hash,
    _json_text,
    _json_value,
    _message_response,
    _normalized_text,
    _owned_conversation,
    _owned_message,
    _payload_hash,
    _server_owned_metadata,
    _title,
)
from .conversations import (
    create_conversation,
    delete_conversation,
    get_conversation,
    list_conversations,
    rename_conversation,
)
from .drafts import (
    _MAX_CAS_RETRIES,
    MAX_HISTORY_LIMIT,
    MAX_IDEMPOTENCY_KEY_LENGTH,
    MAX_MESSAGE_LIMIT,
    MessageRole,
    MessageStatus,
    SortDirection,
    record_draft_confirmation,
    slash_command_definitions,
)
from .messages import (
    _existing_idempotent_message,
    _message_payload,
    append_message,
    get_conversation_detail,
    list_messages,
    list_recent_messages,
    update_message,
)

__all__ = [
    "ChatConflictError",
    "ChatError",
    "ChatNotFoundError",
    "ChatValidationError",
    "MAX_HISTORY_LIMIT",
    "MAX_IDEMPOTENCY_KEY_LENGTH",
    "MAX_MESSAGE_LIMIT",
    "MessageAppendResult",
    "MessageRole",
    "MessageStatus",
    "SortDirection",
    "_MAX_CAS_RETRIES",
    "_as_utc",
    "_conversation_response",
    "_cursor_scope",
    "_decode_cursor",
    "_derived_title",
    "_encode_cursor",
    "_existing_idempotent_message",
    "_group_for",
    "_idempotency_hash",
    "_json_text",
    "_json_value",
    "_message_payload",
    "_message_response",
    "_normalized_text",
    "_owned_conversation",
    "_owned_message",
    "_payload_hash",
    "_server_owned_metadata",
    "_title",
    "append_message",
    "create_conversation",
    "delete_conversation",
    "get_conversation",
    "get_conversation_detail",
    "list_conversations",
    "list_messages",
    "list_recent_messages",
    "record_draft_confirmation",
    "rename_conversation",
    "slash_command_definitions",
    "update_message",
]
