"""Account-scoped conversation persistence and command metadata."""

from .commands import (
    SlashCommandDefinition,
    SlashCommandInvocation,
    SlashCommandRegistry,
    default_slash_command_registry,
    parse_slash_command,
)
from .service import (
    ChatConflictError,
    ChatError,
    ChatNotFoundError,
    ChatValidationError,
    MessageAppendResult,
    append_message,
    create_conversation,
    delete_conversation,
    get_conversation,
    get_conversation_detail,
    list_conversations,
    list_messages,
    rename_conversation,
    update_message,
)

__all__ = [
    "ChatConflictError",
    "ChatError",
    "ChatNotFoundError",
    "ChatValidationError",
    "MessageAppendResult",
    "SlashCommandDefinition",
    "SlashCommandInvocation",
    "SlashCommandRegistry",
    "append_message",
    "create_conversation",
    "default_slash_command_registry",
    "delete_conversation",
    "get_conversation",
    "get_conversation_detail",
    "list_conversations",
    "list_messages",
    "parse_slash_command",
    "rename_conversation",
    "update_message",
]
