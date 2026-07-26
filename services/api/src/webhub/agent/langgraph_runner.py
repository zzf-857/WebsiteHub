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
from typing import Any
from uuid import uuid4

from webhub.chat import service as chat_service
from webhub.chat.service import ChatError
from webhub.config import Settings
from webhub.db.database import Database
from webhub.streaming.ui_message_stream import (
    data_chunk,
    finish_chunk,
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
        sources: list[dict[str, Any]],
    ) -> None:
        try:
            async with self.database.sessions() as session:
                await chat_service.append_message(
                    session,
                    request.account_id,
                    conversation_id,
                    role="assistant",
                    content=text[:MAX_PERSISTED_CONTENT],
                    sources=sources or None,
                    status="complete",
                )
        except ChatError:
            # Losing the archive copy must not corrupt a stream the user has
            # already read; the transcript gap is preferable to a failed turn.
            return

    async def run(self, request: AgentRunRequest) -> AsyncIterator[Mapping[str, Any]]:
        """Stream one turn as UI Message Stream v1 chunks."""

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
        collected: list[str] = []
        sources: list[dict[str, Any]] = []

        async for stream_mode, payload in graph.astream(
            {"messages": [*context.history, HumanMessage(content=request.message)]},
            stream_mode=["messages", "updates"],
            config={"recursion_limit": self.settings.agent_max_steps},
        ):
            if stream_mode == "messages":
                chunk, _ = payload
                if getattr(chunk, "type", None) == "tool":
                    continue
                delta = _message_text(chunk)
                if not delta:
                    continue
                if not text_open:
                    yield text_start_chunk(text_id)
                    text_open = True
                collected.append(delta)
                yield text_delta_chunk(text_id, delta)
            elif stream_mode == "updates":
                for event in _tool_events(payload):
                    if event["kind"] == "call":
                        yield data_chunk("agent-tool-call", event["data"], transient=True)
                    else:
                        sources.append(event["data"])
                        yield data_chunk("agent-tool-result", event["data"])

        if not text_open:
            # A model that answers with tool calls only would otherwise finish
            # with an empty bubble.
            yield text_start_chunk(text_id)
            text_open = True
            collected.append(_EMPTY_REPLY)
            yield text_delta_chunk(text_id, _EMPTY_REPLY)
        yield text_end_chunk(text_id)

        reply = "".join(collected)
        await self._persist_reply(request, context.conversation_id, reply, sources)
        yield finish_chunk(
            finish_reason="stop",
            message_metadata={"conversationId": context.conversation_id},
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
