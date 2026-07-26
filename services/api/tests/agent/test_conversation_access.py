from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from webhub.agent import access as access_module
from webhub.agent.access import DatabaseConversationAccess
from webhub.agent.runner import AgentConversationUnavailableError
from webhub.chat.service import ChatNotFoundError


class FakeDatabase:
    @asynccontextmanager
    async def sessions(self) -> AsyncIterator[object]:
        yield object()


def test_database_conversation_access_uses_authenticated_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, str, str]] = []

    async def fake_get_conversation(session: object, account_id: str, conversation_id: str):
        calls.append((session, account_id, conversation_id))
        return SimpleNamespace(id=conversation_id)

    monkeypatch.setattr(access_module, "get_conversation", fake_get_conversation)
    adapter = DatabaseConversationAccess(FakeDatabase())  # type: ignore[arg-type]

    asyncio.run(
        adapter.assert_owned(
            account_id="account-alice",
            conversation_id="conversation-1",
        )
    )

    assert [(account_id, conversation_id) for _, account_id, conversation_id in calls] == [
        ("account-alice", "conversation-1")
    ]


def test_database_conversation_access_hides_missing_or_foreign_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get_conversation(session: object, account_id: str, conversation_id: str):
        del session, account_id, conversation_id
        raise ChatNotFoundError("会话不存在")

    monkeypatch.setattr(access_module, "get_conversation", fake_get_conversation)
    adapter = DatabaseConversationAccess(FakeDatabase())  # type: ignore[arg-type]

    with pytest.raises(AgentConversationUnavailableError):
        asyncio.run(
            adapter.assert_owned(
                account_id="account-bob",
                conversation_id="conversation-alice",
            )
        )
