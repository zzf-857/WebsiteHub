from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, update

from webhub.chat.models import Conversation, ConversationMessage
from webhub.chat.service import (
    ChatConflictError,
    ChatNotFoundError,
    ChatValidationError,
    append_message,
    create_conversation,
    delete_conversation,
    get_conversation,
    list_conversations,
    list_messages,
    rename_conversation,
    update_message,
)
from webhub.db.database import Database
from webhub.db.migrations import upgrade_database
from webhub.db.models import User, UserPreference


@pytest.fixture
def chat_database(tmp_path: Path) -> Iterator[tuple[Database, Path, dict[str, str]]]:
    database_path = tmp_path / "main.sqlite3"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    upgrade_database(database_url)
    database = Database(database_url)
    users = {"alice": "user-alice", "bob": "user-bob"}

    async def seed() -> None:
        async with database.sessions() as session:
            for username, user_id in users.items():
                session.add(
                    User(
                        id=user_id,
                        username=username,
                        display_name=username.title(),
                        password_hash="test-hash",
                    )
                )
                session.add(UserPreference(user_id=user_id))
            await session.commit()

    asyncio.run(seed())
    try:
        yield database, database_path, users
    finally:
        asyncio.run(database.dispose())


def test_conversation_message_lifecycle_is_idempotent_and_account_scoped(
    chat_database: tuple[Database, Path, dict[str, str]],
) -> None:
    database, database_path, users = chat_database

    async def exercise() -> None:
        async with database.sessions() as session:
            conversation = await create_conversation(session, users["alice"])
            assert conversation.title == "新会话"
            assert conversation.version == 1

            first = await append_message(
                session,
                users["alice"],
                conversation.id,
                role="user",
                content="/搜索   Unity API 文档",
                idempotency_key="request-1",
                expected_version=1,
                sources=[{"type": "library", "siteId": "site-1"}],
                artifacts=[{"type": "search-results", "state": "ready"}],
                metadata={
                    "client": "kept",
                    "slash_command": {"name": "/伪造", "known": True},
                },
            )
            assert first.replayed is False
            assert first.conversation.title == "/搜索 Unity API 文档"
            assert first.conversation.message_count == 1
            assert first.conversation.version == 2
            assert first.message.sequence == 1
            assert first.message.metadata["slash_command"] == {
                "name": "/搜索",
                "argumentText": "Unity API 文档",
                "arguments": ["Unity", "API", "文档"],
                "known": True,
            }

            replay = await append_message(
                session,
                users["alice"],
                conversation.id,
                role="user",
                content="/搜索   Unity API 文档",
                idempotency_key="request-1",
                expected_version=1,
                sources=[{"type": "library", "siteId": "site-1"}],
                artifacts=[{"type": "search-results", "state": "ready"}],
                metadata={
                    "client": "kept",
                    "slash_command": {"name": "/伪造", "known": True},
                },
            )
            assert replay.replayed is True
            assert replay.message.id == first.message.id
            assert replay.conversation.message_count == 1

            with pytest.raises(ChatConflictError) as reused:
                await append_message(
                    session,
                    users["alice"],
                    conversation.id,
                    role="user",
                    content="其他内容",
                    idempotency_key="request-1",
                )
            assert reused.value.code == "idempotency_conflict"

            assistant = await append_message(
                session,
                users["alice"],
                conversation.id,
                role="assistant",
                content="部分答案",
                status="streaming",
                idempotency_key="assistant-1",
                metadata={"slash_command": {"name": "/伪造", "known": True}},
            )
            assert "slash_command" not in assistant.message.metadata
            updated = await update_message(
                session,
                users["alice"],
                conversation.id,
                assistant.message.id,
                expected_version=1,
                content="部分答案（已停止）",
                status="aborted",
            )
            assert updated.status == "aborted"
            assert updated.version == 2

            replayed_assistant = await append_message(
                session,
                users["alice"],
                conversation.id,
                role="assistant",
                content="部分答案",
                status="streaming",
                idempotency_key="assistant-1",
            )
            assert replayed_assistant.replayed is True
            assert replayed_assistant.message.status == "aborted"
            assert replayed_assistant.message.version == 2

            with pytest.raises(ChatConflictError) as stale_message:
                await update_message(
                    session,
                    users["alice"],
                    conversation.id,
                    assistant.message.id,
                    expected_version=1,
                    status="complete",
                )
            assert stale_message.value.code == "version_conflict"

            with pytest.raises(ChatNotFoundError):
                await get_conversation(session, users["bob"], conversation.id)
            with pytest.raises(ChatNotFoundError):
                await list_messages(session, users["bob"], conversation.id)

            current = await get_conversation(session, users["alice"], conversation.id)
            renamed = await rename_conversation(
                session,
                users["alice"],
                conversation.id,
                title="  我的   搜索  ",
                expected_version=current.version,
            )
            assert renamed.title == "我的 搜索"
            assert renamed.title_is_custom is True
            with pytest.raises(ChatConflictError):
                await rename_conversation(
                    session,
                    users["alice"],
                    conversation.id,
                    title="陈旧标题",
                    expected_version=current.version,
                )

            removed = await delete_conversation(
                session,
                users["alice"],
                conversation.id,
                expected_version=renamed.version,
            )
            assert removed.conversation_id == conversation.id
            assert (
                await session.scalar(
                    select(ConversationMessage).where(
                        ConversationMessage.conversation_id == conversation.id
                    )
                )
                is None
            )

    asyncio.run(exercise())

    with sqlite3.connect(database_path) as connection:
        hashes = connection.execute(
            "SELECT idempotency_key_hash FROM conversation_messages"
        ).fetchall()
    assert hashes == []


def test_idempotency_key_is_hashed_and_composite_fk_blocks_cross_account_rows(
    chat_database: tuple[Database, Path, dict[str, str]],
) -> None:
    database, database_path, users = chat_database

    async def create() -> str:
        async with database.sessions() as session:
            conversation = await create_conversation(session, users["alice"])
            await append_message(
                session,
                users["alice"],
                conversation.id,
                role="user",
                content="hello",
                idempotency_key="raw-secret-idempotency-key",
            )
            return conversation.id

    conversation_id = asyncio.run(create())
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        stored_hash = connection.execute(
            "SELECT idempotency_key_hash FROM conversation_messages"
        ).fetchone()[0]
        assert stored_hash == hashlib.sha256(b"raw-secret-idempotency-key").hexdigest()
        assert "raw-secret" not in stored_hash

        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                """
                INSERT INTO conversation_messages(
                    id, user_id, conversation_id, sequence, role, content,
                    parts_json, sources_json, artifacts_json, metadata_json,
                    status, version, payload_hash, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "cross-account-message",
                    users["bob"],
                    conversation_id,
                    2,
                    "user",
                    "forbidden",
                    "[]",
                    "[]",
                    "[]",
                    "{}",
                    "complete",
                    1,
                    "a" * 64,
                    "2026-07-26 00:00:00+00:00",
                    "2026-07-26 00:00:00+00:00",
                ),
            )


def test_history_groups_and_cursors_are_stable_and_account_bound(
    chat_database: tuple[Database, Path, dict[str, str]],
) -> None:
    database, _, users = chat_database
    now = datetime(2026, 7, 26, 12, tzinfo=UTC)
    offsets = (0, 1, 4, 15, 40, 95)

    async def exercise() -> None:
        async with database.sessions() as session:
            conversations = []
            for offset in offsets:
                item = await create_conversation(session, users["alice"], title=f"day-{offset}")
                conversations.append(item)
            for item, offset in zip(conversations, offsets, strict=True):
                value = now - timedelta(days=offset)
                await session.execute(
                    update(Conversation)
                    .where(Conversation.id == item.id)
                    .values(last_message_at=value, updated_at=value)
                )
            await session.commit()

            history = await list_conversations(
                session,
                users["alice"],
                limit=100,
                now=now,
            )
            assert [group.key for group in history.groups] == [
                "today",
                "yesterday",
                "last_7_days",
                "last_30_days",
                "month:2026-06",
                "month:2026-04",
            ]
            assert [group.label for group in history.groups[:4]] == [
                "今天",
                "昨天",
                "近 7 天",
                "近 30 天",
            ]
            assert history.total_count == len(offsets)

            first_page = await list_conversations(
                session,
                users["alice"],
                limit=2,
                now=now,
            )
            assert first_page.next_cursor
            second_page = await list_conversations(
                session,
                users["alice"],
                limit=2,
                cursor=first_page.next_cursor,
                now=now,
            )
            first_titles = [item.title for group in first_page.groups for item in group.items]
            second_titles = [item.title for group in second_page.groups for item in group.items]
            assert first_titles == ["day-0", "day-1"]
            assert second_titles == ["day-4", "day-15"]

            with pytest.raises(ChatValidationError, match="分页游标无效"):
                await list_conversations(
                    session,
                    users["bob"],
                    limit=2,
                    cursor=first_page.next_cursor,
                    now=now,
                )

    asyncio.run(exercise())


def test_history_grouping_uses_bounded_client_timezone_offset(
    chat_database: tuple[Database, Path, dict[str, str]],
) -> None:
    database, _, users = chat_database
    now = datetime(2026, 7, 26, 0, 30, tzinfo=UTC)

    async def exercise() -> None:
        async with database.sessions() as session:
            conversation = await create_conversation(session, users["alice"])
            timestamp = datetime(2026, 7, 25, 18, tzinfo=UTC)
            await session.execute(
                update(Conversation)
                .where(Conversation.id == conversation.id)
                .values(last_message_at=timestamp, updated_at=timestamp)
            )
            await session.commit()

            utc_history = await list_conversations(
                session,
                users["alice"],
                now=now,
                timezone_offset_minutes=0,
            )
            china_history = await list_conversations(
                session,
                users["alice"],
                now=now,
                timezone_offset_minutes=480,
            )
            assert utc_history.groups[0].key == "yesterday"
            assert china_history.groups[0].key == "today"
            with pytest.raises(ChatValidationError, match="时区偏移"):
                await list_conversations(
                    session,
                    users["alice"],
                    now=now,
                    timezone_offset_minutes=841,
                )

    asyncio.run(exercise())


def test_message_json_and_content_limits_are_enforced_before_database_write(
    chat_database: tuple[Database, Path, dict[str, str]],
) -> None:
    database, _, users = chat_database

    async def exercise() -> None:
        async with database.sessions() as session:
            conversation = await create_conversation(session, users["alice"])
            with pytest.raises(ChatValidationError, match="消息内容"):
                await append_message(
                    session,
                    users["alice"],
                    conversation.id,
                    role="user",
                    content="x" * 200_001,
                )
            with pytest.raises(ChatValidationError, match="用户消息不能为空"):
                await append_message(
                    session,
                    users["alice"],
                    conversation.id,
                    role="user",
                    content=" \t\n ",
                )
            with pytest.raises(ChatValidationError, match="artifacts过大"):
                await append_message(
                    session,
                    users["alice"],
                    conversation.id,
                    role="assistant",
                    content="result",
                    artifacts=[{"payload": "x" * (512 * 1024)}],
                )
            with pytest.raises(ChatValidationError, match="不可序列化"):
                await append_message(
                    session,
                    users["alice"],
                    conversation.id,
                    role="assistant",
                    content="result",
                    metadata={"score": float("nan")},
                )
            assert (
                await get_conversation(session, users["alice"], conversation.id)
            ).message_count == 0

    asyncio.run(exercise())
