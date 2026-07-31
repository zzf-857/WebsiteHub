from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from webhub.agent import langgraph_runner as runner_module
from webhub.agent import provider_binding as binding_module
from webhub.agent.langgraph_runner import LangGraphAgentRunner
from webhub.agent.runner import (
    AgentProviderFakeIPError,
    AgentProviderNotConfiguredError,
    AgentRunRequest,
)
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
    turn_id: str | None = None,
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
                turn_id=turn_id or f"test-turn:{request_message}",
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


def _chunk_of_type(
    chunks: Sequence[dict[str, Any]],
    chunk_type: str,
) -> dict[str, Any]:
    return next(chunk for chunk in chunks if chunk["type"] == chunk_type)


def _provider_metadata(chunks: Sequence[dict[str, Any]]) -> dict[str, Any]:
    return next(
        chunk["messageMetadata"]
        for chunk in chunks
        if chunk["type"] == "message-metadata"
        and "provider" in chunk["messageMetadata"]
    )


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
        "message-metadata",
        "data-agent-tool-call",
        "data-agent-tool-result",
        "text-start",
        "text-delta",
        "text-delta",
        "text-end",
        "message-metadata",
        "finish",
    ]
    call_chunk = _chunk_of_type(chunks, "data-agent-tool-call")
    tool_call_id = f"{chunks[0]['messageId']}:tool:1:call-1"
    assert call_chunk["data"] == {
        "toolCallId": tool_call_id,
        "name": "search_library",
        "arguments": {"query": "向量数据库"},
    }
    result_chunk = _chunk_of_type(chunks, "data-agent-tool-result")
    assert result_chunk["data"]["toolCallId"] == tool_call_id
    assert result_chunk["data"]["result"]["source"] == "站内存储数据"

    # The turn is archived in WebHub's own tables, not only in the stream.
    assert [(item.role, item.content) for item in messages] == [
        ("user", "帮我找找向量数据库"),
        ("assistant", "网址库里没有找到相关网站。"),
    ]
    assert messages[1].sources[0]["name"] == "search_library"
    assert messages[1].sources[0]["toolCallId"] == tool_call_id


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
    assert start["messageMetadata"]["turnState"] == "running"
    assert start["messageMetadata"]["turnPersisted"] is False
    provider_metadata = _provider_metadata(chunks)
    assert provider_metadata["provider"] == "ollama"
    assert provider_metadata["model"] == "qwen3"
    # Without a search Provider the model must be told it cannot browse.
    assert provider_metadata["webSearch"] is False
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
        "message-metadata",
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
        "message-metadata",
        "finish",
    ]
    metadata = chunks[-1]["messageMetadata"]
    assert metadata == {
        "turnId": "test-turn:帮我找 RAG 网站",
        "turnState": "complete",
        "messageStatus": "complete",
        "turnPersisted": True,
        "conversationId": chunks[0]["messageMetadata"]["conversationId"],
        "assistantMessageId": chunks[0]["messageMetadata"]["assistantMessageId"],
        "recommendationManifestVersion": 1,
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


def test_account_without_a_model_provider_persists_a_terminal_error(tmp_path: Path) -> None:
    with _account(tmp_path, with_provider=False) as settings:
        chunks, messages = _run(settings, "你好", turn_id="provider-missing-turn")

    assert _types(chunks) == [
        "start",
        "message-metadata",
        "data-agent-error",
        "error",
    ]
    error_metadata = chunks[1]["messageMetadata"]
    assert error_metadata["turnId"] == "provider-missing-turn"
    assert error_metadata["turnState"] == "error"
    assert error_metadata["messageStatus"] == "error"
    assert error_metadata["turnPersisted"] is True
    assert error_metadata["errorCode"] == AgentProviderNotConfiguredError.code
    assert chunks[2]["data"] == {
        "code": AgentProviderNotConfiguredError.code,
        "message": AgentProviderNotConfiguredError.safe_message,
    }
    assert [(message.role, message.status) for message in messages] == [
        ("user", "complete"),
        ("assistant", "error"),
    ]
    assert messages[-1].metadata == error_metadata


def test_fake_ip_failure_persists_and_replays_a_precise_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolve_calls = 0

    async def reject_fake_ip(*args: object, **kwargs: object) -> None:
        nonlocal resolve_calls
        del args, kwargs
        resolve_calls += 1
        raise AgentProviderFakeIPError("不得持久化的内部诊断")

    monkeypatch.setattr(runner_module, "resolve_binding", reject_fake_ip)

    with _account(tmp_path) as settings:
        async def create_conversation() -> str:
            database = Database(settings.database_url)
            try:
                async with database.sessions() as session:
                    user_id = await session.scalar(select(User.id))
                    assert user_id is not None
                    conversation = await chat_service.create_conversation(
                        session,
                        user_id,
                        title="Provider 诊断",
                    )
                    return conversation.id
            finally:
                await database.dispose()

        conversation_id = asyncio.run(create_conversation())
        chunks, messages = _run(
            settings,
            "你好",
            conversation_id=conversation_id,
            turn_id="provider-fake-ip-turn",
        )
        replayed, _ = _run(
            settings,
            "你好",
            conversation_id=conversation_id,
            turn_id="provider-fake-ip-turn",
        )

    error_metadata = chunks[1]["messageMetadata"]
    assert error_metadata["errorCode"] == AgentProviderFakeIPError.code
    assert chunks[2]["data"] == {
        "code": AgentProviderFakeIPError.code,
        "message": AgentProviderFakeIPError.safe_message,
    }
    assert "不得持久化的内部诊断" not in str(chunks)
    assert messages[-1].metadata == error_metadata
    assert _chunk_of_type(replayed, "data-agent-error")["data"] == chunks[2]["data"]
    assert "不得持久化的内部诊断" not in str(replayed)
    assert resolve_calls == 1


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
    assert _provider_metadata(chunks)["webSearch"] is False
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
    assert _provider_metadata(chunks)["webSearch"] is False
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
    assert _provider_metadata(chunks)["webSearch"] is True
    tool_names = [tool.name for tool in graph.captured["tools"]]  # type: ignore[attr-defined]
    assert "web_search" in tool_names


def test_web_search_sources_are_trusted_normalized_deduplicated_and_attributed() -> None:
    tool_result = {
        "toolCallId": "call-search",
        "name": "web_search",
        "result": {
            "source": "联网搜索",
            "provider_id": "tavily",
            "items": [
                {
                    "title": "  Example   Docs  ",
                    "url": "https://Example.com/docs#first",
                },
                {
                    "title": "duplicate",
                    "url": "https://example.com:443/docs#second",
                },
                {
                    "title": "secret",
                    "url": "https://example.com/docs?token=secret",
                },
                {"title": "private", "url": "http://127.0.0.1/internal"},
            ],
        },
    }
    seen_urls: set[str] = set()

    chunks = runner_module._source_url_chunks(tool_result, seen_urls)

    canonical_url = "https://example.com/docs"
    assert chunks == [
        {
            "type": "source-url",
            "sourceId": f"web:{hashlib.sha256(canonical_url.encode()).hexdigest()[:24]}",
            "url": canonical_url,
            "title": "Example Docs",
            "providerMetadata": {"webhub": {"searchProvider": "tavily"}},
        }
    ]
    assert seen_urls == {canonical_url}
    assert runner_module._source_url_chunks(tool_result, seen_urls) == []

    for untrusted in (
        {**tool_result, "name": "search_library"},
        {
            **tool_result,
            "result": {**tool_result["result"], "source": "模型知识"},
        },
        {
            **tool_result,
            "result": {**tool_result["result"], "provider_id": "unknown"},
        },
    ):
        assert runner_module._source_url_chunks(untrusted, set()) == []


def test_in_progress_notification_does_not_reuse_the_durable_assistant_id() -> None:
    async def collect() -> list[Mapping[str, Any]]:
        request = AgentRunRequest(
            account_id="account-alice",
            turn_id="turn-live",
            conversation_id="conversation-1",
            message="同一问题",
        )
        claim = SimpleNamespace(
            action="in_progress",
            state="running",
            assistant_message_id="assistant-durable",
            conversation_id="conversation-1",
            retry_after_seconds=12,
        )
        return [chunk async for chunk in runner_module._turn_status_stream(request, claim)]

    chunks = asyncio.run(collect())
    assert chunks[0]["messageId"] != "assistant-durable"
    assert chunks[0]["messageMetadata"]["assistantMessageId"] == "assistant-durable"
    assert chunks[0]["messageMetadata"]["retryAfterSeconds"] == 12


def test_incomplete_assistant_turn_is_removed_from_next_model_history() -> None:
    items = [
        SimpleNamespace(
            role="user",
            status="complete",
            content="这个问题后来被停止",
            metadata={"turnId": "turn-aborted"},
            artifacts=[],
        ),
        SimpleNamespace(
            role="assistant",
            status="aborted",
            content="只回答了一半",
            metadata={"turnId": "turn-aborted"},
            artifacts=[],
        ),
        SimpleNamespace(
            role="user",
            status="complete",
            content="完整问题",
            metadata={"turnId": "turn-complete"},
            artifacts=[],
        ),
        SimpleNamespace(
            role="assistant",
            status="complete",
            content="完整回答",
            metadata={"turnId": "turn-complete"},
            artifacts=[],
        ),
    ]

    history = runner_module._history_messages(items)

    assert [message.content for message in history] == ["完整问题", "完整回答"]


def test_same_turn_retry_replays_without_reinvoking_graph_or_duplicate_messages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lost client response must be recoverable by the same stable turn id."""

    with _account(tmp_path) as settings:
        graph = _install_fake_graph(monkeypatch, _text_events(["只生成一次。\n"]))
        first_chunks, first_messages = _run(
            settings,
            "幂等回合",
            turn_id="stable-turn-1",
        )
        replay_chunks, replay_messages = _run(
            settings,
            "幂等回合",
            turn_id="stable-turn-1",
        )

    assert _types(first_chunks)[-1] == "finish"
    assert _types(replay_chunks) == [
        "start",
        "text-start",
        "text-delta",
        "text-end",
        "finish",
    ]
    assert replay_chunks[0]["messageMetadata"]["turnReplayed"] is True
    assert len(graph.calls) == 1
    assert [(item.role, item.content) for item in first_messages] == [
        ("user", "幂等回合"),
        ("assistant", "只生成一次。\n"),
    ]
    assert [(item.role, item.content) for item in replay_messages] == [
        ("user", "幂等回合"),
        ("assistant", "只生成一次。\n"),
    ]


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
