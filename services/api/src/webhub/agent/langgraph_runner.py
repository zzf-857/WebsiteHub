"""The real ``AgentRunner``: LangGraph execution rendered as a UI Message Stream.

This is the adapter the route has been waiting for.  It owns four jobs:

1. resolve the account's own Provider credentials (never a built-in key);
2. replay the conversation from the WebHub tables so history survives restarts;
3. translate LangGraph events into AI SDK UI Message Stream v1 chunks;
4. persist the finished assistant turn.

Failure handling is deliberately blunt.  Anything unexpected propagates as
``AgentProviderNotConfiguredError`` or a bare exception, and the route's
``_guard_runner_source`` turns it into a generic error chunk — vendor
exception text (which routinely embeds URLs, request bodies and key
fragments) must never reach the browser.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from time import perf_counter
from typing import Any
from uuid import uuid4

from webhub.chat import service as chat_service
from webhub.chat.service import ChatError
from webhub.config import Settings
from webhub.db.database import Database
from webhub.streaming.ui_message_stream import (
    data_chunk,
    finish_chunk,
    reasoning_delta_chunk,
    reasoning_end_chunk,
    reasoning_start_chunk,
    start_chunk,
    text_delta_chunk,
    text_end_chunk,
    text_start_chunk,
)

from .graph import build_agent_graph
from .prompt import build_system_prompt
from .provider_binding import (
    ProviderBinding,
    build_chat_model,
    resolve_binding,
    resolve_optional_binding,
)
from .runner import AgentProviderNotConfiguredError, AgentRunRequest
from .tools import AgentToolContext, build_tools

MAX_PERSISTED_CONTENT = 32_000
MAX_PERSISTED_REASONING = 32_000
_EMPTY_REPLY = "（本轮没有生成任何内容，请换一种说法再试一次。）"


def _message_text(message: Any) -> str:
    """Extract plain text from a LangChain message across content shapes.

    v1 messages may carry a ``str`` content, a list of content blocks, or
    expose a ``text`` property/method depending on the sub-class.
    """

    text = getattr(message, "text", None)
    # Check ``str`` first: langchain-core ships a compatibility shim whose
    # ``text`` is both a string and callable, and calling it warns.
    if isinstance(text, str):
        return text
    if callable(text):
        try:
            called = text()
        except TypeError:
            called = None
        if isinstance(called, str):
            return called

    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, Mapping) and block.get("type") == "text":
                value = block.get("text")
                if isinstance(value, str):
                    parts.append(value)
        return "".join(parts)
    return ""


def _message_reasoning(message: Any) -> str:
    """Extract only Provider-declared reasoning from a LangChain chunk."""

    try:
        blocks = getattr(message, "content_blocks", None)
    except (AttributeError, TypeError, ValueError):
        blocks = None
    if isinstance(blocks, Sequence) and not isinstance(blocks, (str, bytes, bytearray)):
        parts = [
            block.get("reasoning")
            for block in blocks
            if isinstance(block, Mapping)
            and block.get("type") == "reasoning"
            and isinstance(block.get("reasoning"), str)
        ]
        if parts:
            return "".join(parts)

    additional = getattr(message, "additional_kwargs", None)
    if isinstance(additional, Mapping):
        reasoning = additional.get("reasoning_content")
        if isinstance(reasoning, str):
            return reasoning
    return ""


def _message_usage(message: Any) -> dict[str, int]:
    """Project Provider-reported LangChain usage without estimating missing fields."""

    raw = getattr(message, "usage_metadata", None)
    if not isinstance(raw, Mapping):
        return {}
    result: dict[str, int] = {}
    for source, target in (
        ("input_tokens", "inputTokens"),
        ("output_tokens", "outputTokens"),
        ("total_tokens", "totalTokens"),
    ):
        value = raw.get(source)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            result[target] = value
    details = raw.get("output_token_details")
    if isinstance(details, Mapping):
        reasoning = details.get("reasoning")
        if isinstance(reasoning, int) and not isinstance(reasoning, bool) and reasoning >= 0:
            result["reasoningTokens"] = reasoning
    return result


def _add_usage(total: dict[str, int], delta: Mapping[str, int]) -> None:
    for key, value in delta.items():
        total[key] = total.get(key, 0) + value


def _merge_usage_max(total: dict[str, int], observed: Mapping[str, int]) -> None:
    """Keep one model round's latest cumulative Provider counters."""

    for key, value in observed.items():
        total[key] = max(total.get(key, 0), value)


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    """Reduce a tool payload to something the SSE encoder can serialize."""

    if depth > 6:
        return None
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item, depth=depth + 1) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_safe(item, depth=depth + 1) for item in value]
    return str(value)


def _tool_payload(content: Any) -> Any:
    if isinstance(content, str):
        try:
            return _json_safe(json.loads(content))
        except (TypeError, ValueError):
            return content[:2_000]
    return _json_safe(content)


def _web_search_declined(metadata: Mapping[str, Any]) -> bool:
    """Return True only when the client explicitly switched web search off.

    Client hints may narrow a turn's capabilities but never widen them: the
    account's own Provider config decides whether browsing exists at all, so
    ``webSearch: true`` from the browser grants nothing.  ``metadata`` is
    untrusted input — anything but a strict boolean ``False`` (the string
    "false", 0, None, ...) counts as "no preference", not as a vote.
    """

    return metadata.get("webSearch") is False


@dataclass(frozen=True, slots=True)
class _TurnContext:
    conversation_id: str
    history: list[Any]
    model_binding: ProviderBinding
    search_binding: ProviderBinding | None


@dataclass(frozen=True, slots=True)
class LangGraphAgentRunner:
    """Account-scoped runner backed by LangGraph + the user's own Provider."""

    database: Database
    settings: Settings

    async def _resolve_bindings(
        self, user_id: str, *, allow_web_search: bool
    ) -> tuple[ProviderBinding, ProviderBinding | None]:
        async with self.database.sessions() as session:
            model_binding = await resolve_binding(
                session,
                self.settings,
                user_id=user_id,
                kind="model",
            )
            # Skipped when the user opted out, so a turn that will never browse
            # does not decrypt a search key or re-resolve its hostname.
            search_binding = (
                await resolve_optional_binding(
                    session,
                    self.settings,
                    user_id=user_id,
                    kind="search",
                )
                if allow_web_search
                else None
            )
        return model_binding, search_binding

    async def _prepare_turn(self, request: AgentRunRequest) -> _TurnContext:
        # Narrow-only: the hint can turn browsing off for this turn, never on.
        declined = _web_search_declined(request.metadata)
        model_binding, search_binding = await self._resolve_bindings(
            request.account_id,
            allow_web_search=not declined,
        )

        history: list[Any] = []
        async with self.database.sessions() as session:
            conversation_id = request.conversation_id
            if conversation_id is None:
                conversation = await chat_service.create_conversation(
                    session,
                    request.account_id,
                    title=None,
                )
                conversation_id = conversation.id
            else:
                recent = await chat_service.list_recent_messages(
                    session,
                    request.account_id,
                    conversation_id,
                    limit=self.settings.agent_history_messages,
                )
                history = _history_messages(recent)

            # Persist the user's turn before calling out, so an aborted or
            # failed model call still leaves a faithful transcript.
            await chat_service.append_message(
                session,
                request.account_id,
                conversation_id,
                role="user",
                content=request.message,
                metadata=(
                    {"slashCommand": request.slash_command.metadata()}
                    if request.slash_command is not None
                    else None
                ),
            )
        return _TurnContext(
            conversation_id=conversation_id,
            history=history,
            model_binding=model_binding,
            search_binding=search_binding,
        )

    async def _persist_reply(
        self,
        request: AgentRunRequest,
        conversation_id: str,
        text: str,
        reasoning: str,
        sources: list[dict[str, Any]],
        metadata: dict[str, Any],
    ) -> None:
        parts: list[dict[str, str]] = []
        if reasoning:
            parts.append(
                {
                    "type": "reasoning",
                    "text": reasoning[:MAX_PERSISTED_REASONING],
                }
            )
        parts.append({"type": "text", "text": text[:MAX_PERSISTED_CONTENT]})
        try:
            async with self.database.sessions() as session:
                await chat_service.append_message(
                    session,
                    request.account_id,
                    conversation_id,
                    role="assistant",
                    content=text[:MAX_PERSISTED_CONTENT],
                    parts=parts,
                    sources=sources or None,
                    metadata=metadata,
                    status="complete",
                )
        except ChatError:
            # Losing the archive copy must not corrupt a stream the user has
            # already read; the transcript gap is preferable to a failed turn.
            return

    async def run(self, request: AgentRunRequest) -> AsyncIterator[Mapping[str, Any]]:
        """Stream one turn as UI Message Stream v1 chunks."""

        run_started = perf_counter()
        context = await self._prepare_turn(request)
        message_id = f"assistant-{uuid4()}"
        yield start_chunk(
            message_id=message_id,
            message_metadata={
                "conversationId": context.conversation_id,
                "provider": context.model_binding.provider,
                "model": context.model_binding.model_name,
                # The effective capability after narrowing, not the request hint.
                "webSearch": context.search_binding is not None,
            },
        )

        tool_context = AgentToolContext(
            database=self.database,
            settings=self.settings,
            user_id=request.account_id,
            search_binding=context.search_binding,
        )
        graph = build_agent_graph(
            model=build_chat_model(context.model_binding),
            tools=build_tools(tool_context),
            system_prompt=build_system_prompt(
                slash_command=request.slash_command,
                web_search_available=context.search_binding is not None,
                web_search_declined=_web_search_declined(request.metadata),
            ),
        )

        from langchain_core.messages import HumanMessage

        text_id = f"text-{uuid4()}"
        text_open = False
        reasoning_id: str | None = None
        reasoning_open = False
        reasoning_started: float | None = None
        reasoning_elapsed = 0.0
        first_output_at: float | None = None
        collected: list[str] = []
        collected_reasoning: list[str] = []
        sources: list[dict[str, Any]] = []
        usage_rounds: list[dict[str, int]] = [{}]

        async for stream_mode, payload in graph.astream(
            {"messages": [*context.history, HumanMessage(content=request.message)]},
            stream_mode=["messages", "updates"],
            config={"recursion_limit": self.settings.agent_max_steps},
        ):
            if stream_mode == "messages":
                chunk, _ = payload
                if getattr(chunk, "type", None) == "tool":
                    continue
                # Provider usage is normally emitted once at the end of a
                # model round, but compatible endpoints may repeat cumulative
                # counters on several chunks.  Take the maximum within a round
                # and only sum distinct rounds separated by tool events.
                _merge_usage_max(usage_rounds[-1], _message_usage(chunk))
                reasoning = _message_reasoning(chunk)
                if reasoning:
                    now = perf_counter()
                    if first_output_at is None:
                        first_output_at = now
                    if not reasoning_open:
                        reasoning_id = f"reasoning-{uuid4()}"
                        yield reasoning_start_chunk(reasoning_id)
                        reasoning_open = True
                        reasoning_started = now
                    collected_reasoning.append(reasoning)
                    yield reasoning_delta_chunk(reasoning_id, reasoning)
                delta = _message_text(chunk)
                if not delta:
                    continue
                now = perf_counter()
                if first_output_at is None:
                    first_output_at = now
                if reasoning_open and reasoning_id is not None:
                    yield reasoning_end_chunk(reasoning_id)
                    reasoning_open = False
                    if reasoning_started is not None:
                        reasoning_elapsed += now - reasoning_started
                    reasoning_started = None
                if not text_open:
                    yield text_start_chunk(text_id)
                    text_open = True
                collected.append(delta)
                yield text_delta_chunk(text_id, delta)
            elif stream_mode == "updates":
                tool_events = _tool_events(payload)
                if tool_events and usage_rounds[-1]:
                    usage_rounds.append({})
                if tool_events and reasoning_open and reasoning_id is not None:
                    now = perf_counter()
                    yield reasoning_end_chunk(reasoning_id)
                    reasoning_open = False
                    if reasoning_started is not None:
                        reasoning_elapsed += now - reasoning_started
                    reasoning_started = None
                for event in tool_events:
                    if event["kind"] == "call":
                        yield data_chunk("agent-tool-call", event["data"], transient=True)
                    else:
                        sources.append(event["data"])
                        yield data_chunk("agent-tool-result", event["data"])

        if reasoning_open and reasoning_id is not None:
            now = perf_counter()
            yield reasoning_end_chunk(reasoning_id)
            reasoning_open = False
            if reasoning_started is not None:
                reasoning_elapsed += now - reasoning_started
        if not text_open:
            # A model that answers with tool calls only would otherwise finish
            # with an empty bubble.
            yield text_start_chunk(text_id)
            text_open = True
            collected.append(_EMPTY_REPLY)
            if first_output_at is None:
                first_output_at = perf_counter()
            yield text_delta_chunk(text_id, _EMPTY_REPLY)
        yield text_end_chunk(text_id)

        reply = "".join(collected)
        finished_at = perf_counter()
        usage: dict[str, int] = {}
        for round_usage in usage_rounds:
            _add_usage(usage, round_usage)
        finish_metadata: dict[str, Any] = {
            "conversationId": context.conversation_id,
            "provider": context.model_binding.provider,
            "model": context.model_binding.model_name,
            "webSearch": context.search_binding is not None,
            "elapsedMs": max(0, round((finished_at - run_started) * 1_000)),
        }
        if first_output_at is not None:
            finish_metadata["timeToFirstTokenMs"] = max(
                0,
                round((first_output_at - run_started) * 1_000),
            )
        if reasoning_elapsed > 0:
            finish_metadata["reasoningMs"] = max(0, round(reasoning_elapsed * 1_000))
        if usage:
            finish_metadata["usage"] = usage
        await self._persist_reply(
            request,
            context.conversation_id,
            reply,
            "".join(collected_reasoning),
            sources,
            finish_metadata,
        )
        yield finish_chunk(
            finish_reason="stop",
            message_metadata=finish_metadata,
        )


def _history_messages(items: Sequence[Any]) -> list[Any]:
    """Convert persisted conversation rows into LangChain messages.

    Only message *content* is replayed — tool calls and their results are not.
    That is why confirmed drafts have to be written back as their own ``system``
    row (see ``chat.service.record_draft_confirmation``): a tool result frozen
    at ``awaiting_confirmation`` would otherwise be invisible here, and the
    assistant's own prose ("请确认后保存") is the only thing the next turn sees.
    """

    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    messages: list[Any] = []
    for item in items:
        if not item.content:
            continue
        if item.role == "user":
            messages.append(HumanMessage(content=item.content))
        elif item.role == "assistant" and item.status == "complete":
            messages.append(AIMessage(content=item.content))
        elif item.role == "system":
            # Server-composed facts about what the user confirmed.  These are
            # never authored by the model or the browser.
            messages.append(SystemMessage(content=item.content))
    return messages


def _tool_events(payload: Any) -> list[dict[str, Any]]:
    """Extract tool calls and tool results from one LangGraph state update."""

    events: list[dict[str, Any]] = []
    if not isinstance(payload, Mapping):
        return events
    for node_state in payload.values():
        if not isinstance(node_state, Mapping):
            continue
        messages = node_state.get("messages")
        if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
            continue
        for message in messages:
            message_type = getattr(message, "type", None)
            if message_type == "tool":
                events.append(
                    {
                        "kind": "result",
                        "data": {
                            "toolCallId": getattr(message, "tool_call_id", "") or "",
                            "name": getattr(message, "name", "") or "",
                            "result": _tool_payload(getattr(message, "content", "")),
                        },
                    }
                )
                continue
            for call in getattr(message, "tool_calls", None) or []:
                if not isinstance(call, Mapping):
                    continue
                events.append(
                    {
                        "kind": "call",
                        "data": {
                            "toolCallId": str(call.get("id") or ""),
                            "name": str(call.get("name") or ""),
                            "arguments": _json_safe(call.get("args")),
                        },
                    }
                )
    return events


def build_agent_runner(database: Database, settings: Settings) -> LangGraphAgentRunner:
    return LangGraphAgentRunner(database=database, settings=settings)


__all__ = [
    "AgentProviderNotConfiguredError",
    "LangGraphAgentRunner",
    "build_agent_runner",
]
