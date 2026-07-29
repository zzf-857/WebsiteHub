from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, update

from webhub.agent.runner import AgentRunRequest
from webhub.agent.turns import (
    TURN_ABORTED_CODE,
    TURN_EXPIRED_CODE,
    AgentTurnJournal,
    AgentTurnLeaseLostError,
    AgentTurnMessages,
    claim_turn,
    close_expired_turns,
    finish_claimed_turn,
    load_turn_assistant,
    mark_turn_terminal,
)
from webhub.chat import service as chat_service
from webhub.db.database import Database
from webhub.db.migrations import upgrade_database
from webhub.db.models import User
from webhub.db.models._base import utc_now
from webhub.db.models.agent_turns import AgentTurnRun


@pytest.fixture
def turn_database(tmp_path: Path) -> Iterator[tuple[Database, str]]:
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'main.sqlite3').as_posix()}"
    upgrade_database(database_url)
    database = Database(database_url)
    user_id = "00000000-0000-0000-0000-000000000001"

    async def seed() -> None:
        async with database.sessions() as session:
            session.add(
                User(
                    id=user_id,
                    username="turn-test-user",
                    display_name="Turn Test User",
                    password_hash="test-hash",
                )
            )
            await session.commit()

    asyncio.run(seed())
    try:
        yield database, user_id
    finally:
        asyncio.run(database.dispose())


def _request(user_id: str, *, turn_id: str = "turn-1", message: str = "hello") -> AgentRunRequest:
    return AgentRunRequest(
        account_id=user_id,
        turn_id=turn_id,
        conversation_id=None,
        message=message,
    )


def test_same_turn_concurrent_claim_has_one_executor_and_one_in_progress(
    turn_database: tuple[Database, str],
) -> None:
    database, user_id = turn_database

    async def exercise() -> None:
        request = _request(user_id)
        claims = await asyncio.gather(
            claim_turn(database, request),
            claim_turn(database, request),
        )
        assert sorted(claim.action for claim in claims) == ["execute", "in_progress"]
        assert len({claim.run_id for claim in claims}) == 1

    asyncio.run(exercise())


def test_same_turn_different_payload_is_a_conflict(
    turn_database: tuple[Database, str],
) -> None:
    database, user_id = turn_database

    async def exercise() -> None:
        first = await claim_turn(database, _request(user_id, message="first"))
        assert first.action == "execute"
        conflict = await claim_turn(database, _request(user_id, message="second"))
        assert conflict.action == "conflict"
        assert conflict.run_id == first.run_id

    asyncio.run(exercise())


def test_complete_turn_is_replayed_without_a_new_lease(
    turn_database: tuple[Database, str],
) -> None:
    database, user_id = turn_database

    async def exercise() -> None:
        request = _request(user_id)
        first = await claim_turn(database, request)
        assert first.action == "execute" and first.lease is not None
        await mark_turn_terminal(database, first.lease, state="complete", error_code=None)

        replay = await claim_turn(database, request)
        assert replay.action == "replay"
        assert replay.state == "complete"
        assert replay.lease is None

        async with database.sessions() as session:
            run = await session.scalar(select(AgentTurnRun).where(AgentTurnRun.id == first.run_id))
        assert run is not None
        assert run.attempt_count == 1
        assert run.lease_token_hash is None

    asyncio.run(exercise())


def test_first_turn_retry_accepts_the_server_assigned_conversation_id(
    turn_database: tuple[Database, str],
) -> None:
    database, user_id = turn_database

    async def exercise() -> None:
        message = "first turn"
        first = AgentRunRequest(
            account_id=user_id,
            turn_id="first-turn-retry",
            conversation_id=None,
            message=message,
            idempotency_payload={
                "message": message,
                "conversationId": None,
                "slashCommand": None,
                "metadata": {},
            },
        )
        claim = await claim_turn(database, first)
        assert claim.action == "execute" and claim.lease is not None

        async with database.sessions() as session:
            conversation = await chat_service.create_conversation(session, user_id)
            user = await chat_service.append_message(
                session,
                user_id,
                conversation.id,
                role="user",
                content=message,
            )
            assistant = await chat_service.append_message(
                session,
                user_id,
                conversation.id,
                role="assistant",
                content="",
                metadata={"turnId": first.turn_id, "messageStatus": "streaming"},
                status="streaming",
            )

        from webhub.agent.turns import bind_turn_messages

        await bind_turn_messages(
            database,
            claim.lease,
            AgentTurnMessages(
                conversation_id=conversation.id,
                user_message_id=user.message.id,
                assistant_message_id=assistant.message.id,
                assistant_version=assistant.message.version,
            ),
        )
        await finish_claimed_turn(
            database,
            claim.lease,
            turn_id=first.turn_id,
            state="complete",
            metadata={"messageStatus": "complete"},
            error_code=None,
        )

        retry = AgentRunRequest(
            account_id=user_id,
            turn_id=first.turn_id,
            conversation_id=conversation.id,
            message=message,
            idempotency_payload={
                "message": message,
                "conversationId": conversation.id,
                "slashCommand": None,
                "metadata": {},
            },
        )
        replay = await claim_turn(database, retry)
        assert replay.action == "replay"
        assert replay.conversation_id == conversation.id

    asyncio.run(exercise())


@pytest.mark.parametrize("terminal", [False, True])
def test_conversation_deletion_preserves_turn_tombstone_and_prevents_reexecution(
    turn_database: tuple[Database, str],
    terminal: bool,
) -> None:
    database, user_id = turn_database

    async def exercise() -> None:
        request = _request(
            user_id,
            turn_id=f"deleted-conversation-{'complete' if terminal else 'running'}",
        )
        claim = await claim_turn(database, request)
        assert claim.action == "execute" and claim.lease is not None

        async with database.sessions() as session:
            conversation = await chat_service.create_conversation(session, user_id)
            user = await chat_service.append_message(
                session,
                user_id,
                conversation.id,
                role="user",
                content=request.message,
            )
            assistant = await chat_service.append_message(
                session,
                user_id,
                conversation.id,
                role="assistant",
                content="done" if terminal else "partial",
                metadata={"turnId": request.turn_id, "messageStatus": "streaming"},
                status="streaming",
            )

        from webhub.agent.turns import bind_turn_messages

        await bind_turn_messages(
            database,
            claim.lease,
            AgentTurnMessages(
                conversation_id=conversation.id,
                user_message_id=user.message.id,
                assistant_message_id=assistant.message.id,
                assistant_version=assistant.message.version,
            ),
        )
        if terminal:
            await finish_claimed_turn(
                database,
                claim.lease,
                turn_id=request.turn_id,
                state="complete",
                metadata={"messageStatus": "complete"},
                error_code=None,
            )

        async with database.sessions() as session:
            await chat_service.delete_conversation(
                session,
                user_id,
                conversation.id,
                expected_version=assistant.conversation.version,
            )
            run = await session.scalar(
                select(AgentTurnRun).where(AgentTurnRun.id == claim.run_id)
            )
        assert run is not None
        assert run.requested_conversation_id is None
        assert run.conversation_id is None
        assert run.user_message_id is None
        assert run.assistant_message_id is None

        retry = await claim_turn(database, request)
        if terminal:
            assert retry.action == "replay"
            assert retry.state == "complete"
            return

        assert retry.action == "in_progress"
        async with database.sessions() as session:
            await session.execute(
                update(AgentTurnRun)
                .where(AgentTurnRun.id == claim.run_id)
                .values(lease_expires_at=utc_now() - timedelta(seconds=1))
            )
            await session.commit()
        assert await close_expired_turns(database, user_id=user_id) == 1
        replay = await claim_turn(database, request)
        assert replay.action == "replay"
        assert replay.state == "aborted"

    asyncio.run(exercise())


def test_claimed_turn_can_abort_before_message_placeholders_exist(
    turn_database: tuple[Database, str],
) -> None:
    database, user_id = turn_database

    async def exercise() -> None:
        request = _request(user_id, turn_id="early-abort")
        claim = await claim_turn(database, request)
        assert claim.action == "execute" and claim.lease is not None

        message = await finish_claimed_turn(
            database,
            claim.lease,
            turn_id=request.turn_id,
            state="aborted",
            metadata={"messageStatus": "aborted"},
            error_code=TURN_ABORTED_CODE,
        )
        assert message is None

        replay = await claim_turn(database, request)
        assert replay.action == "replay"
        assert replay.state == "aborted"
        assert replay.error_code == TURN_ABORTED_CODE

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("state", "error_code"),
    [("aborted", TURN_ABORTED_CODE), ("error", "runner_unavailable")],
)
def test_terminal_states_persist_partial_text_reasoning_sources_and_metadata(
    turn_database: tuple[Database, str],
    state: str,
    error_code: str,
) -> None:
    database, user_id = turn_database

    async def exercise() -> None:
        async with database.sessions() as session:
            conversation = await chat_service.create_conversation(session, user_id)
            user = await chat_service.append_message(
                session,
                user_id,
                conversation.id,
                role="user",
                content="保留部分回答",
                idempotency_key="turn-user",
            )
            assistant = await chat_service.append_message(
                session,
                user_id,
                conversation.id,
                role="assistant",
                content="",
                parts=[],
                sources=[],
                metadata={"turnId": "partial-turn", "messageStatus": "streaming"},
                status="streaming",
                idempotency_key="turn-assistant",
            )

        request = AgentRunRequest(
            account_id=user_id,
            turn_id="partial-turn",
            conversation_id=conversation.id,
            message="保留部分回答",
        )
        claim = await claim_turn(database, request)
        assert claim.action == "execute" and claim.lease is not None
        messages = AgentTurnMessages(
            conversation_id=conversation.id,
            user_message_id=user.message.id,
            assistant_message_id=assistant.message.id,
            assistant_version=assistant.message.version,
        )
        from webhub.agent.turns import bind_turn_messages

        await bind_turn_messages(database, claim.lease, messages)
        journal = AgentTurnJournal(
            database=database,
            lease=claim.lease,
            turn_id=request.turn_id,
            messages=messages,
            metadata={"conversationId": conversation.id},
        )
        journal.add_reasoning("先分析。")
        journal.add_text("已生成的部分。")
        journal.add_tool_result(
            {
                "toolCallId": "tool-1",
                "name": "web_search",
                "result": {"source": "联网搜索", "items": []},
            }
        )
        journal.add_source(
            {
                "type": "source-url",
                "sourceId": "web:source-1",
                "url": "https://example.com/",
                "providerMetadata": {"webhub": {"searchProvider": "tavily"}},
            }
        )
        await journal.finish(state, metadata={"reasoningMs": 42}, error_code=error_code)  # type: ignore[arg-type]

        persisted = await load_turn_assistant(
            database,
            user_id=user_id,
            message_id=assistant.message.id,
        )
        assert persisted is not None
        assert persisted.status == state
        assert persisted.content == "已生成的部分。"
        assert {part["type"] for part in persisted.parts} == {"reasoning", "source-url", "text"}
        assert persisted.parts[0] == {"type": "reasoning", "text": "先分析。"}
        assert persisted.sources[0]["name"] == "web_search"
        assert persisted.metadata["messageStatus"] == state
        assert persisted.metadata["reasoningMs"] == 42
        if state == "error":
            assert persisted.metadata["errorCode"] == error_code

        async with database.sessions() as session:
            run = await session.scalar(select(AgentTurnRun).where(AgentTurnRun.id == claim.run_id))
        assert run is not None
        assert run.state == state
        assert run.error_code == (None if state == "complete" else error_code)
        assert run.lease_token_hash is None
        assert run.lease_expires_at is None

    asyncio.run(exercise())


def test_expired_running_turn_is_closed_and_partial_message_remains_visible(
    turn_database: tuple[Database, str],
) -> None:
    database, user_id = turn_database

    async def exercise() -> None:
        async with database.sessions() as session:
            conversation = await chat_service.create_conversation(session, user_id)
            user = await chat_service.append_message(
                session,
                user_id,
                conversation.id,
                role="user",
                content="过期回合",
                idempotency_key="expired-user",
            )
            assistant = await chat_service.append_message(
                session,
                user_id,
                conversation.id,
                role="assistant",
                content="已经输出一半",
                parts=[{"type": "text", "text": "已经输出一半"}],
                metadata={"turnId": "expired-turn", "messageStatus": "streaming"},
                status="streaming",
                idempotency_key="expired-assistant",
            )
        request = AgentRunRequest(
            account_id=user_id,
            turn_id="expired-turn",
            conversation_id=conversation.id,
            message="过期回合",
        )
        claim = await claim_turn(database, request)
        assert claim.action == "execute" and claim.lease is not None
        from webhub.agent.turns import bind_turn_messages

        await bind_turn_messages(
            database,
            claim.lease,
            AgentTurnMessages(
                conversation_id=conversation.id,
                user_message_id=user.message.id,
                assistant_message_id=assistant.message.id,
                assistant_version=assistant.message.version,
            ),
        )
        stale_journal = AgentTurnJournal(
            database=database,
            lease=claim.lease,
            turn_id=request.turn_id,
            messages=AgentTurnMessages(
                conversation_id=conversation.id,
                user_message_id=user.message.id,
                assistant_message_id=assistant.message.id,
                assistant_version=assistant.message.version,
            ),
            metadata={"conversationId": conversation.id},
        )
        async with database.sessions() as session:
            await session.execute(
                update(AgentTurnRun)
                .where(AgentTurnRun.id == claim.run_id)
                .values(lease_expires_at=utc_now() - timedelta(seconds=1))
            )
            await session.commit()

        assert await close_expired_turns(database, user_id=user_id) == 1
        persisted = await load_turn_assistant(
            database,
            user_id=user_id,
            message_id=assistant.message.id,
        )
        assert persisted is not None
        assert persisted.status == "aborted"
        assert persisted.content == "已经输出一半"
        assert persisted.metadata["errorCode"] == TURN_EXPIRED_CODE

        stale_journal.add_text("这段来自已失效的旧执行器")
        with pytest.raises(AgentTurnLeaseLostError):
            await stale_journal.checkpoint(force=True)
        unchanged = await load_turn_assistant(
            database,
            user_id=user_id,
            message_id=assistant.message.id,
        )
        assert unchanged is not None
        assert unchanged.status == "aborted"
        assert unchanged.content == "已经输出一半"

        async with database.sessions() as session:
            run = await session.scalar(select(AgentTurnRun).where(AgentTurnRun.id == claim.run_id))
        assert run is not None
        assert run.state == "aborted"
        assert run.error_code == TURN_EXPIRED_CODE

    asyncio.run(exercise())
