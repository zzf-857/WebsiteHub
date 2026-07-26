from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from webhub.agent.routes import router
from webhub.agent.runner import (
    AgentConversationUnavailableError,
    AgentProviderNotConfiguredError,
    AgentRunRequest,
)
from webhub.agent.schemas import (
    MAX_AGENT_METADATA_BYTES,
    AgentChatRequest,
)
from webhub.auth.dependencies import require_current_identity, require_trusted_origin
from webhub.streaming.ui_message_stream import (
    finish_chunk,
    start_chunk,
    text_delta_chunk,
    text_end_chunk,
    text_start_chunk,
)


def _chunks(response: Any) -> list[dict[str, Any] | str]:
    events = [event for event in response.text.split("\n\n") if event]
    return [
        "[DONE]" if event == "data: [DONE]" else json.loads(event.removeprefix("data: "))
        for event in events
    ]


class RecordingRunner:
    def __init__(self) -> None:
        self.requests: list[AgentRunRequest] = []

    def run(self, request: AgentRunRequest):
        self.requests.append(request)
        return [
            start_chunk(message_id="assistant-1"),
            text_start_chunk("text-1"),
            text_delta_chunk("text-1", "结果"),
            text_end_chunk("text-1"),
            finish_chunk(finish_reason="stop"),
        ]


class RecordingConversationAccess:
    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls: list[tuple[str, str]] = []

    async def assert_owned(self, *, account_id: str, conversation_id: str) -> None:
        self.calls.append((account_id, conversation_id))
        if not self.allowed:
            raise AgentConversationUnavailableError


@contextmanager
def _client(
    *,
    account_id: str = "account-alice",
    runner: object | None = None,
    access: object | None = None,
) -> Iterator[TestClient]:
    app = FastAPI()
    if runner is not None:
        app.state.agent_runner = runner
    if access is not None:
        app.state.agent_conversation_access = access

    async def identity_override():
        return SimpleNamespace(user=SimpleNamespace(id=account_id))

    app.dependency_overrides[require_current_identity] = identity_override
    app.dependency_overrides[require_trusted_origin] = lambda: None
    app.include_router(router, prefix="/api")
    with TestClient(app) as client:
        yield client


def test_route_scopes_runner_and_conversation_to_authenticated_account() -> None:
    runner = RecordingRunner()
    access = RecordingConversationAccess()
    with _client(runner=runner, access=access) as client:
        response = client.post(
            "/api/agent/chat",
            json={
                "conversationId": "conversation-1",
                "message": "/搜索 Unity API",
                "slashCommand": {
                    "name": "/搜索",
                    "argumentText": "Unity API",
                    "arguments": ["Unity", "API"],
                    "known": True,
                },
                "metadata": {"searchScope": "collection"},
            },
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache, no-store"
    assert response.headers["x-vercel-ai-ui-message-stream"] == "v1"
    assert response.headers["x-webhub-agent-protocol"] == "v1"
    assert access.calls == [("account-alice", "conversation-1")]
    assert len(runner.requests) == 1
    request = runner.requests[0]
    assert request.account_id == "account-alice"
    assert request.user_id == "account-alice"
    assert request.conversation_id == "conversation-1"
    assert request.message == "/搜索 Unity API"
    assert request.metadata == {"searchScope": "collection"}
    assert request.slash_command is not None
    assert request.slash_command.known is True
    assert request.slash_command.definition is not None
    assert request.slash_command.definition.name == "/搜索"
    assert request.slash_command.arguments == ("Unity", "API")
    assert [chunk["type"] for chunk in _chunks(response)[:-1]] == [
        "start",
        "text-start",
        "text-delta",
        "text-end",
        "finish",
    ]
    assert _chunks(response)[-1] == "[DONE]"


def test_account_scope_cannot_be_overridden_by_request_body() -> None:
    runner = RecordingRunner()
    with _client(runner=runner) as client:
        response = client.post(
            "/api/agent/chat",
            json={
                "message": "hello",
                "account_id": "account-bob",
            },
        )

    assert response.status_code == 422
    assert runner.requests == []


def test_client_metadata_is_json_bounded_and_cannot_supply_identity() -> None:
    runner = RecordingRunner()
    with _client(runner=runner) as client:
        oversized = client.post(
            "/api/agent/chat",
            json={
                "message": "hello",
                "metadata": {"nested": {"text": "x" * MAX_AGENT_METADATA_BYTES}},
            },
        )
        forged_identity = client.post(
            "/api/agent/chat",
            json={"message": "hello", "metadata": {"accountId": "account-bob"}},
        )

    assert oversized.status_code == 422
    assert forged_identity.status_code == 422
    assert runner.requests == []
    with pytest.raises(ValidationError, match="JSON serializable"):
        AgentChatRequest(message="hello", metadata={"bad": object()})


def test_existing_conversation_fails_closed_without_access_adapter() -> None:
    runner = RecordingRunner()
    with _client(runner=runner) as client:
        response = client.post(
            "/api/agent/chat",
            json={"conversation_id": "conversation-foreign", "message": "hello"},
        )

    chunks = _chunks(response)
    assert response.status_code == 200
    assert runner.requests == []
    assert chunks[1] == {
        "type": "data-agent-error",
        "data": {
            "code": "conversation_unavailable",
            "message": AgentConversationUnavailableError.safe_message,
        },
        "transient": True,
    }
    assert chunks[-2] == {
        "type": "error",
        "errorText": AgentConversationUnavailableError.safe_message,
    }
    assert chunks[-1] == "[DONE]"


def test_conversation_access_failure_is_sanitized_and_blocks_runner() -> None:
    secret = "database-url-with-sensitive-query"
    runner = RecordingRunner()

    class BrokenConversationAccess:
        async def assert_owned(self, *, account_id: str, conversation_id: str) -> None:
            del account_id, conversation_id
            raise RuntimeError(secret)

    with _client(runner=runner, access=BrokenConversationAccess()) as client:
        response = client.post(
            "/api/agent/chat",
            json={"conversation_id": "conversation-1", "message": "hello"},
        )

    chunks = _chunks(response)
    assert runner.requests == []
    assert secret not in response.text
    assert chunks[1]["data"]["code"] == "conversation_access_unavailable"
    assert chunks[-1] == "[DONE]"


def test_default_runner_returns_safe_provider_error_without_external_call() -> None:
    with _client() as client:
        response = client.post("/api/agent/chat", json={"message": "hello"})

    chunks = _chunks(response)
    assert response.status_code == 200
    assert chunks[0]["type"] == "start"
    assert chunks[0]["messageMetadata"] == {"errorCode": "provider_not_configured"}
    assert chunks[1]["data"] == {
        "code": "provider_not_configured",
        "message": AgentProviderNotConfiguredError.safe_message,
    }
    assert chunks[-2]["errorText"] == AgentProviderNotConfiguredError.safe_message
    assert chunks[-1] == "[DONE]"


def test_unknown_command_is_not_forwarded_or_reflected() -> None:
    runner = RecordingRunner()
    secret_argument = "sk-never-reflect-this"
    with _client(runner=runner) as client:
        response = client.post(
            "/api/agent/chat",
            json={"message": f"/不存在 {secret_argument}"},
        )

    chunks = _chunks(response)
    assert runner.requests == []
    assert chunks[1]["data"]["code"] == "unknown_slash_command"
    assert secret_argument not in response.text
    assert chunks[-1] == "[DONE]"


def test_mismatched_command_metadata_is_rejected_before_runner() -> None:
    runner = RecordingRunner()
    with _client(runner=runner) as client:
        response = client.post(
            "/api/agent/chat",
            json={
                "message": "/搜索 Unity",
                "slash_command": {
                    "name": "/存入",
                    "argumentText": "https://example.com",
                },
            },
        )

    chunks = _chunks(response)
    assert runner.requests == []
    assert chunks[1]["data"]["code"] == "invalid_slash_command"
    assert chunks[-1] == "[DONE]"


def test_explicit_command_metadata_has_an_encoded_size_budget() -> None:
    runner = RecordingRunner()
    with _client(runner=runner) as client:
        response = client.post(
            "/api/agent/chat",
            json={
                "message": "/搜索 Unity",
                "slashCommand": {
                    "name": "/搜索",
                    "arguments": ["x" * 4_000 for _ in range(20)],
                    "known": True,
                },
            },
        )

    assert response.status_code == 422
    assert runner.requests == []


def test_provider_error_raised_during_stream_is_sanitized() -> None:
    secret = "provider-secret-that-must-not-leak"

    class StreamingFailureRunner:
        def run(self, request: AgentRunRequest):
            del request

            async def chunks():
                yield start_chunk(message_id="assistant-partial")
                raise AgentProviderNotConfiguredError(secret)

            return chunks()

    with _client(runner=StreamingFailureRunner()) as client:
        response = client.post("/api/agent/chat", json={"message": "hello"})

    chunks = _chunks(response)
    assert secret not in response.text
    assert chunks[-3]["data"]["code"] == "provider_not_configured"
    assert chunks[-2]["errorText"] == AgentProviderNotConfiguredError.safe_message
    assert chunks[-1] == "[DONE]"


def test_unexpected_runner_construction_error_is_sanitized() -> None:
    secret = "https://api.example.test?key=sk-sensitive"

    class BrokenRunner:
        def run(self, request: AgentRunRequest):
            del request
            raise RuntimeError(secret)

    with _client(runner=BrokenRunner()) as client:
        response = client.post("/api/agent/chat", json={"message": "hello"})

    chunks = _chunks(response)
    assert secret not in response.text
    assert chunks[1]["data"]["code"] == "runner_unavailable"
    assert chunks[-1] == "[DONE]"


@pytest.mark.parametrize(
    "invalid_source",
    [
        {"type": "finish"},
        "secret-string-source",
        b"secret-bytes-source",
    ],
)
def test_runner_must_return_a_chunk_collection_not_one_iterable_value(
    invalid_source: object,
) -> None:
    class InvalidSourceRunner:
        def run(self, request: AgentRunRequest):
            del request
            return invalid_source

    with _client(runner=InvalidSourceRunner()) as client:
        response = client.post("/api/agent/chat", json={"message": "hello"})

    chunks = _chunks(response)
    assert "secret" not in response.text
    assert chunks[1]["data"]["code"] == "runner_unavailable"
    assert chunks[-1] == "[DONE]"
