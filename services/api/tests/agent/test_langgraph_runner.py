from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from webhub.agent import langgraph_runner as runner_module
from webhub.agent import provider_binding as binding_module
from webhub.agent.langgraph_runner import LangGraphAgentRunner
from webhub.agent.runner import AgentProviderNotConfiguredError, AgentRunRequest
from webhub.chat import service as chat_service
from webhub.config import Settings
from webhub.db.database import Database
from webhub.db.migrations import upgrade_database
from webhub.db.models import User
from webhub.main import create_app
from webhub.streaming.ui_message_stream import encode_ui_message_stream

ORIGIN = {"Origin": "http://testserver"}
MASTER_KEY = b"provider-test-master-key-32bytes"
OLLAMA_BASE_URL = "http://127.0.0.1:11434"


@contextmanager
def _account(
    tmp_path: Path,
    *,
    with_provider: bool = True,
    with_search: bool = False,
) -> Iterator[Settings]:
    database_path = tmp_path / "main.sqlite3"
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        data_directory=tmp_path,
        provider_master_key=MASTER_KEY,
    )
    upgrade_database(settings.database_url)
    with TestClient(create_app(settings=settings)) as client:
        registered = client.post(
            "/api/auth/register",
            json={"username": "alice", "password": "a sufficiently secure password"},
            headers=ORIGIN,
        )
        assert registered.status_code == 201, registered.text
        if with_provider:
            created = client.post(
                "/api/providers",
                json={
                    "kind": "model",
                    "provider": "ollama",
                    "display_name": "本地 Ollama",
                    "base_url": OLLAMA_BASE_URL,
                    "model_name": "qwen3",
                    "enabled": True,
                },
                headers=ORIGIN,
            )
            assert created.status_code == 201, created.text
        if with_search:
            search_created = client.post(
                "/api/providers",
                json={
                    "kind": "search",
                    "provider": "tavily",
                    "display_name": "Tavily 搜索",
                    "secret": {"action": "write", "value": "tvly-test-secret"},
                    "enabled": True,
                },
                headers=ORIGIN,
            )
            assert search_created.status_code == 201, search_created.text
    yield settings


class _FakeGraph:
    """Stands in for the compiled LangGraph; emits a scripted event script."""

    def __init__(self, events: Sequence[tuple[str, Any]]) -> None:
        self.events = list(events)
        self.calls: list[dict[str, Any]] = []

    def astream(self, state: Any, **kwargs: Any) -> AsyncIterator[tuple[str, Any]]:
        self.calls.append({"state": state, **kwargs})

        async def iterator() -> AsyncIterator[tuple[str, Any]]:
            for event in self.events:
                yield event

        return iterator()


def _install_fake_graph(
    monkeypatch: pytest.MonkeyPatch,
    events: Sequence[tuple[str, Any]],
) -> _FakeGraph:
    graph = _FakeGraph(events)
    captured: dict[str, Any] = {}

    def fake_build_graph(*, model: Any, tools: Any, system_prompt: str) -> _FakeGraph:
        captured.update({"model": model, "tools": tools, "system_prompt": system_prompt})
        return graph

    monkeypatch.setattr(runner_module, "build_agent_graph", fake_build_graph)
    monkeypatch.setattr(
        runner_module,
        "build_chat_model",
        lambda binding: f"model:{binding.provider}",
    )
    graph.captured = captured  # type: ignore[attr-defined]
    return graph


def _text_events(chunks: Sequence[str]) -> list[tuple[str, Any]]:
    from langchain_core.messages import AIMessageChunk

    return [("messages", (AIMessageChunk(content=chunk), {})) for chunk in chunks]


def _allow_all_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the DNS-level SSRF re-check: tests must never touch the network."""

    async def allow_target(*args: object, **kwargs: object) -> None:
        del args, kwargs

    monkeypatch.setattr(binding_module, "validate_connection_target", allow_target)


def _run(
    settings: Settings,
    request_message: str,
    conversation_id: str | None = None,
    metadata: Mapping[str, Any] | None = None,
):
    async def scenario():
        database = Database(settings.database_url)
        try:
            async with database.sessions() as session:
                user_id = await session.scalar(select(User.id))
            assert user_id is not None
            runner = LangGraphAgentRunner(database=database, settings=settings)
            request = AgentRunRequest(
                account_id=user_id,
                conversation_id=conversation_id,
                message=request_message,
                metadata=dict(metadata) if metadata is not None else {},
            )
            chunks = [chunk async for chunk in runner.run(request)]
            async with database.sessions() as session:
                conversations = await chat_service.list_conversations(session, user_id)
                messages = await chat_service.list_messages(
                    session,
                    user_id,
                    conversations.groups[0].items[0].id,
                )
            return chunks, messages.items
        finally:
            await database.dispose()

    return asyncio.run(scenario())


def _types(chunks: Sequence[dict[str, Any]]) -> list[str]:
    return [str(chunk["type"]) for chunk in chunks]


def test_streams_text_tool_calls_and_persists_the_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from langchain_core.messages import AIMessage, ToolMessage

    tool_call = {"id": "call-1", "name": "search_library", "args": {"query": "向量数据库"}}
    events = [
        (
            "updates",
            {"agent": {"messages": [AIMessage(content="", tool_calls=[tool_call])]}},
        ),
        (
            "updates",
            {
                "tools": {
                    "messages": [
                        ToolMessage(
                            content=json.dumps(
                                {"source": "站内存储数据", "items": []},
                                ensure_ascii=False,
                            ),
                            tool_call_id="call-1",
                            name="search_library",
                        )
                    ]
                }
            },
        ),
        *_text_events(["网址库里", "没有找到相关网站。"]),
    ]
    with _account(tmp_path) as settings:
        _install_fake_graph(monkeypatch, events)
        chunks, messages = _run(settings, "帮我找找向量数据库")

    assert _types(chunks) == [
        "start",
        "data-agent-tool-call",
        "data-agent-tool-result",
        "text-start",
        "text-delta",
        "text-delta",
        "text-end",
        "finish",
    ]
    call_chunk = chunks[1]
    assert call_chunk["data"] == {
        "toolCallId": "call-1",
        "name": "search_library",
        "arguments": {"query": "向量数据库"},
    }
    result_chunk = chunks[2]
    assert result_chunk["data"]["result"]["source"] == "站内存储数据"

    # The turn is archived in WebHub's own tables, not only in the stream.
    assert [(item.role, item.content) for item in messages] == [
        ("user", "帮我找找向量数据库"),
        ("assistant", "网址库里没有找到相关网站。"),
    ]
    assert messages[1].sources[0]["name"] == "search_library"


def test_new_conversation_id_is_announced_in_stream_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _account(tmp_path) as settings:
        _install_fake_graph(monkeypatch, _text_events(["好的。"]))
        chunks, _ = _run(settings, "你好")

    start = chunks[0]
    conversation_id = start["messageMetadata"]["conversationId"]
    assert conversation_id
    assert start["messageMetadata"]["provider"] == "ollama"
    assert start["messageMetadata"]["model"] == "qwen3"
    # Without a search Provider the model must be told it cannot browse.
    assert start["messageMetadata"]["webSearch"] is False
    finish_metadata = chunks[-1]["messageMetadata"]
    assert finish_metadata["conversationId"] == conversation_id
    assert "usage" not in finish_metadata
    assert "reasoningMs" not in finish_metadata


def test_reasoning_usage_and_server_timings_stream_and_persist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

    tool_call = {"id": "call-1", "name": "search_library", "args": {"query": "RAG"}}
    events = [
        (
            "messages",
            (
                AIMessageChunk(
                    content="",
                    additional_kwargs={"reasoning_content": "先查网址库。"},
                ),
                {},
            ),
        ),
        (
            "updates",
            {"agent": {"messages": [AIMessage(content="", tool_calls=[tool_call])] }},
        ),
        (
            "updates",
            {
                "tools": {
                    "messages": [
                        ToolMessage(
                            content='{"items":[]}',
                            tool_call_id="call-1",
                            name="search_library",
                        )
                    ]
                }
            },
        ),
        (
            "messages",
            (
                AIMessageChunk(
                    content="",
                    additional_kwargs={"reasoning_content": "再综合结果。"},
                ),
                {},
            ),
        ),
        (
            "messages",
            (
                AIMessageChunk(
                    content="",
                    usage_metadata={
                        "input_tokens": 20,
                        "output_tokens": 8,
                        "total_tokens": 28,
                        "output_token_details": {"reasoning": 5},
                    },
                ),
                {},
            ),
        ),
        (
            "messages",
            (
                AIMessageChunk(
                    content="网址库暂时没有匹配项。",
                    usage_metadata={
                        "input_tokens": 20,
                        "output_tokens": 8,
                        "total_tokens": 28,
                        "output_token_details": {"reasoning": 5},
                    },
                ),
                {},
            ),
        ),
    ]
    clock = iter([1.0, 1.2, 1.5, 1.7, 2.0, 2.1])
    monkeypatch.setattr(runner_module, "perf_counter", lambda: next(clock))

    with _account(tmp_path) as settings:
        _install_fake_graph(monkeypatch, events)
        chunks, messages = _run(settings, "帮我找 RAG 网站")

    assert _types(chunks) == [
        "start",
        "reasoning-start",
        "reasoning-delta",
        "reasoning-end",
        "data-agent-tool-call",
        "data-agent-tool-result",
        "reasoning-start",
        "reasoning-delta",
        "reasoning-end",
        "text-start",
        "text-delta",
        "text-end",
        "finish",
    ]
    metadata = chunks[-1]["messageMetadata"]
    assert metadata == {
        "conversationId": chunks[0]["messageMetadata"]["conversationId"],
        "provider": "ollama",
        "model": "qwen3",
        "webSearch": False,
        "elapsedMs": 1100,
        "timeToFirstTokenMs": 200,
        "reasoningMs": 600,
        "usage": {
            "inputTokens": 20,
            "outputTokens": 8,
            "totalTokens": 28,
            "reasoningTokens": 5,
        },
    }
    assistant = messages[-1]
    assert assistant.metadata == metadata
    assert assistant.parts == [
        {"type": "reasoning", "text": "先查网址库。再综合结果。"},
        {"type": "text", "text": "网址库暂时没有匹配项。"},
    ]


def test_tool_only_answer_still_produces_a_visible_reply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from langchain_core.messages import AIMessage

    events = [
        (
            "updates",
            {
                "agent": {
                    "messages": [
                        AIMessage(
                            content="",
                            tool_calls=[{"id": "c1", "name": "list_tags", "args": {}}],
                        )
                    ]
                }
            },
        ),
    ]
    with _account(tmp_path) as settings:
        _install_fake_graph(monkeypatch, events)
        chunks, messages = _run(settings, "看看标签")

    assert "text-start" in _types(chunks)
    assert messages[1].content.strip()


def test_stream_is_valid_for_the_ui_message_encoder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _account(tmp_path) as settings:
        _install_fake_graph(monkeypatch, _text_events(["一", "二"]))
        chunks, _ = _run(settings, "数数")

    async def encode() -> bytes:
        async def source():
            for chunk in chunks:
                yield chunk

        return b"".join(
            [payload async for payload in encode_ui_message_stream(source(), recover_errors=False)]
        )

    encoded = asyncio.run(encode())
    assert encoded.endswith(b"data: [DONE]\n\n")
    assert b'"errorText"' not in encoded


def test_account_without_a_model_provider_cannot_start_a_turn(tmp_path: Path) -> None:
    with _account(tmp_path, with_provider=False) as settings:
        pass

    async def scenario() -> None:
        database = Database(settings.database_url)
        try:
            async with database.sessions() as session:
                user_id = await session.scalar(select(User.id))
            assert user_id is not None
            runner = LangGraphAgentRunner(database=database, settings=settings)
            request = AgentRunRequest(
                account_id=user_id,
                conversation_id=None,
                message="你好",
            )
            async for _ in runner.run(request):
                pass
        finally:
            await database.dispose()

    with pytest.raises(AgentProviderNotConfiguredError):
        asyncio.run(scenario())


def test_client_can_switch_off_a_configured_search_provider_for_one_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _account(tmp_path, with_search=True) as settings:
        _allow_all_targets(monkeypatch)
        graph = _install_fake_graph(monkeypatch, _text_events(["好的。"]))
        chunks, _ = _run(settings, "只看我的收藏", metadata={"webSearch": False})

    # The stream advertises the effective capability, and the graph never even
    # receives the web_search tool for this turn.
    assert chunks[0]["messageMetadata"]["webSearch"] is False
    tool_names = [tool.name for tool in graph.captured["tools"]]  # type: ignore[attr-defined]
    assert "web_search" not in tool_names


def test_client_cannot_enable_web_search_the_account_never_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _account(tmp_path) as settings:
        graph = _install_fake_graph(monkeypatch, _text_events(["好的。"]))
        chunks, _ = _run(settings, "帮我联网查", metadata={"webSearch": True})

    # metadata can only narrow: without an account-level search Provider the
    # hint grants nothing.
    assert chunks[0]["messageMetadata"]["webSearch"] is False
    tool_names = [tool.name for tool in graph.captured["tools"]]  # type: ignore[attr-defined]
    assert "web_search" not in tool_names


def test_non_boolean_web_search_hint_is_treated_as_no_preference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _account(tmp_path, with_search=True) as settings:
        _allow_all_targets(monkeypatch)
        graph = _install_fake_graph(monkeypatch, _text_events(["好的。"]))
        chunks, _ = _run(settings, "查一下", metadata={"webSearch": "false"})

    # The string "false" is not a strict boolean, so the account default wins.
    assert chunks[0]["messageMetadata"]["webSearch"] is True
    tool_names = [tool.name for tool in graph.captured["tools"]]  # type: ignore[attr-defined]
    assert "web_search" in tool_names


def test_history_replay_keeps_the_newest_turns_not_the_oldest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long conversation must not silently lose its recent context."""

    with _account(tmp_path) as settings:
        window = settings.agent_history_messages

        async def seed() -> str:
            database = Database(settings.database_url)
            try:
                async with database.sessions() as session:
                    user_id = await session.scalar(select(User.id))
                    assert user_id is not None
                    conversation = await chat_service.create_conversation(
                        session, user_id, title=None
                    )
                    # Comfortably more than one window, so a forward-paginating
                    # read would return only "第 1 轮"-era messages.
                    for index in range(window * 2):
                        await chat_service.append_message(
                            session,
                            user_id,
                            conversation.id,
                            role="user" if index % 2 == 0 else "assistant",
                            content=f"第 {index} 条",
                            status="complete",
                        )
                return conversation.id
            finally:
                await database.dispose()

        conversation_id = asyncio.run(seed())
        graph = _install_fake_graph(monkeypatch, _text_events(["好的。"]))
        _run(settings, "接着上面说", conversation_id=conversation_id)

    replayed = [
        message.content
        for message in graph.calls[0]["state"]["messages"]
        if getattr(message, "type", None) in {"human", "ai"}
    ]
    assert f"第 {window * 2 - 1} 条" in replayed
    assert "第 0 条" not in replayed
    # The window is honoured: history plus the new user turn.
    assert len(replayed) == window + 1
