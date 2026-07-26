"""Conversation ownership adapter for the Agent HTTP route."""

from __future__ import annotations

from dataclasses import dataclass

from webhub.chat.service import ChatNotFoundError, get_conversation
from webhub.db.database import Database

from .runner import AgentConversationUnavailableError


@dataclass(frozen=True, slots=True)
class DatabaseConversationAccess:
    """Check conversation ownership through a short-lived database session."""

    database: Database

    async def assert_owned(self, *, account_id: str, conversation_id: str) -> None:
        async with self.database.sessions() as session:
            try:
                await get_conversation(session, account_id, conversation_id)
            except ChatNotFoundError as error:
                raise AgentConversationUnavailableError from error


__all__ = ["DatabaseConversationAccess"]
