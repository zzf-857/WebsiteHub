"""Account-scoped Agent chat stream route.

This router is intentionally standalone.  The main application must register
it and inject a conversation access adapter plus a real ``AgentRunner`` in a
later integration slice.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterable, AsyncIterator, Iterable, Mapping
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from webhub.auth.dependencies import (
    CurrentIdentityDependency,
    require_trusted_origin,
)
from webhub.chat.commands import (
    SlashCommandInvocation,
    SlashCommandRegistry,
    default_slash_command_registry,
    parse_slash_command,
)
from webhub.streaming.ui_message_stream import (
    data_chunk,
    error_chunk,
    start_chunk,
    ui_message_stream_response,
)

from .runner import (
    AgentChunkSource,
    AgentConversationAccess,
    AgentConversationUnavailableError,
    AgentProviderNotConfiguredError,
    AgentRunner,
    AgentRunRequest,
    RejectingConversationAccess,
    UnconfiguredAgentRunner,
)
from .schemas import AgentChatRequest

router = APIRouter(prefix="/agent", tags=["agent"])
WriteOriginDependency = Annotated[None, Depends(require_trusted_origin)]
DEFAULT_AGENT_SLASH_COMMAND_REGISTRY = default_slash_command_registry()

AGENT_PROTOCOL_HEADER = "x-webhub-agent-protocol"
AGENT_PROTOCOL_VERSION = "v1"
UNKNOWN_COMMAND_MESSAGE = "不支持的命令，请从命令列表中选择。"
COMMAND_METADATA_MESSAGE = "命令元数据与消息不一致。"
RUNNER_FAILURE_MESSAGE = "Agent 暂时无法完成请求，请稍后重试。"
CONVERSATION_ACCESS_FAILURE_MESSAGE = "会话校验暂时不可用，请稍后重试。"


def get_agent_runner(request: Request) -> AgentRunner:
    """Resolve the process/application runner without making a fake call."""

    runner = getattr(request.app.state, "agent_runner", None)
    return runner if runner is not None else UnconfiguredAgentRunner()


def get_conversation_access(request: Request) -> AgentConversationAccess:
    """Resolve the account-scope authorizer, failing closed by default."""

    access = getattr(request.app.state, "agent_conversation_access", None)
    return access if access is not None else RejectingConversationAccess()


def get_slash_command_registry(request: Request) -> SlashCommandRegistry:
    registry = getattr(request.app.state, "agent_slash_command_registry", None)
    return registry if registry is not None else DEFAULT_AGENT_SLASH_COMMAND_REGISTRY


AgentRunnerDependency = Annotated[AgentRunner, Depends(get_agent_runner)]
AgentConversationAccessDependency = Annotated[
    AgentConversationAccess,
    Depends(get_conversation_access),
]
SlashCommandRegistryDependency = Annotated[
    SlashCommandRegistry,
    Depends(get_slash_command_registry),
]


def _command_metadata_matches(
    invocation: SlashCommandInvocation,
    payload: AgentChatRequest,
) -> bool:
    explicit = payload.slash_command
    if explicit is None:
        return True
    return all(
        (
            explicit.name is None or explicit.name == invocation.name,
            explicit.argument_text is None
            or explicit.argument_text.strip() == invocation.argument_text,
            explicit.arguments is None or explicit.arguments == invocation.arguments,
            explicit.known is None or explicit.known is invocation.known,
        )
    )


def _stream_error(
    *,
    code: str,
    message: str,
    conversation_id: str | None,
) -> StreamingResponse:
    """Return a protocol-level error that AI SDK clients can render safely."""

    async def chunks() -> AsyncIterator[Mapping[str, Any]]:
        metadata: dict[str, Any] = {"errorCode": code}
        if conversation_id is not None:
            metadata["conversationId"] = conversation_id
        yield start_chunk(message_id=f"assistant-{uuid4()}", message_metadata=metadata)
        yield data_chunk(
            "agent-error",
            {"code": code, "message": message},
            transient=True,
        )
        yield error_chunk(message)

    return ui_message_stream_response(
        chunks(),
        headers={AGENT_PROTOCOL_HEADER: AGENT_PROTOCOL_VERSION},
    )


async def _await_runner_result(result: object) -> AgentChunkSource:
    if inspect.isawaitable(result):
        result = await result
    # The codec performs the detailed shape validation.  Keeping this check
    # here gives adapters a useful type error before a response is constructed.
    if isinstance(result, (Mapping, str, bytes, bytearray, memoryview)) or not isinstance(
        result,
        (AsyncIterable, Iterable),
    ):
        raise TypeError("AgentRunner.run must return an iterable of UI message chunks")
    return result


async def _guard_runner_source(
    source: AgentChunkSource,
) -> AsyncIterator[Mapping[str, Any]]:
    """Convert known provider failures into safe typed stream errors.

    Unknown runner failures become a generic application error without
    exposing provider exception text.  The shared encoder remains a final
    safety net for malformed chunks or lifecycle violations.
    """

    try:
        if isinstance(source, AsyncIterable):
            async for chunk in source:
                yield chunk
        else:
            for chunk in source:
                yield chunk
    except AgentProviderNotConfiguredError:
        yield data_chunk(
            "agent-error",
            {
                "code": AgentProviderNotConfiguredError.code,
                "message": AgentProviderNotConfiguredError.safe_message,
            },
            transient=True,
        )
        yield error_chunk(AgentProviderNotConfiguredError.safe_message)
    except AgentConversationUnavailableError:
        # A runner must not disclose another account's conversation details.
        yield data_chunk(
            "agent-error",
            {
                "code": AgentConversationUnavailableError.code,
                "message": AgentConversationUnavailableError.safe_message,
            },
            transient=True,
        )
        yield error_chunk(AgentConversationUnavailableError.safe_message)
    except Exception:
        # Do not include exception text: provider errors commonly contain URLs,
        # request bodies, or credential fragments.
        yield data_chunk(
            "agent-error",
            {"code": "runner_unavailable", "message": RUNNER_FAILURE_MESSAGE},
            transient=True,
        )
        yield error_chunk(RUNNER_FAILURE_MESSAGE)


@router.post("/chat", response_class=StreamingResponse)
async def chat(
    payload: AgentChatRequest,
    identity: CurrentIdentityDependency,
    runner: AgentRunnerDependency,
    conversation_access: AgentConversationAccessDependency,
    command_registry: SlashCommandRegistryDependency,
    _: WriteOriginDependency,
) -> StreamingResponse:
    """Start one account-scoped Agent turn as an AI SDK UI Message stream."""

    try:
        slash_command = parse_slash_command(payload.message, registry=command_registry)
    except (TypeError, ValueError):
        return _stream_error(
            code="invalid_slash_command",
            message=COMMAND_METADATA_MESSAGE,
            conversation_id=payload.conversation_id,
        )
    if slash_command.is_command and not slash_command.known:
        return _stream_error(
            code="unknown_slash_command",
            message=UNKNOWN_COMMAND_MESSAGE,
            conversation_id=payload.conversation_id,
        )
    if not _command_metadata_matches(slash_command, payload):
        return _stream_error(
            code="invalid_slash_command",
            message=COMMAND_METADATA_MESSAGE,
            conversation_id=payload.conversation_id,
        )

    account_id = str(identity.user.id)
    if payload.conversation_id is not None:
        try:
            await conversation_access.assert_owned(
                account_id=account_id,
                conversation_id=payload.conversation_id,
            )
        except AgentConversationUnavailableError:
            return _stream_error(
                code=AgentConversationUnavailableError.code,
                message=AgentConversationUnavailableError.safe_message,
                conversation_id=payload.conversation_id,
            )
        except Exception:
            return _stream_error(
                code="conversation_access_unavailable",
                message=CONVERSATION_ACCESS_FAILURE_MESSAGE,
                conversation_id=payload.conversation_id,
            )

    run_request = AgentRunRequest(
        account_id=account_id,
        conversation_id=payload.conversation_id,
        message=payload.message,
        slash_command=slash_command if slash_command.is_command else None,
        metadata=dict(payload.metadata),
    )
    try:
        source = await _await_runner_result(runner.run(run_request))
    except AgentProviderNotConfiguredError:
        return _stream_error(
            code=AgentProviderNotConfiguredError.code,
            message=AgentProviderNotConfiguredError.safe_message,
            conversation_id=payload.conversation_id,
        )
    except AgentConversationUnavailableError:
        return _stream_error(
            code=AgentConversationUnavailableError.code,
            message=AgentConversationUnavailableError.safe_message,
            conversation_id=payload.conversation_id,
        )
    except Exception:
        # Runner construction errors are handled before the stream starts; keep
        # provider details out of both status and response body.
        return _stream_error(
            code="runner_unavailable",
            message=RUNNER_FAILURE_MESSAGE,
            conversation_id=payload.conversation_id,
        )

    return ui_message_stream_response(
        _guard_runner_source(source),
        headers={AGENT_PROTOCOL_HEADER: AGENT_PROTOCOL_VERSION},
    )


__all__ = [
    "AGENT_PROTOCOL_HEADER",
    "AGENT_PROTOCOL_VERSION",
    "AgentConversationAccessDependency",
    "AgentRunnerDependency",
    "SlashCommandRegistryDependency",
    "get_agent_runner",
    "get_conversation_access",
    "get_slash_command_registry",
    "router",
]
