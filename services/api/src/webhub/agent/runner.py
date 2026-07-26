"""Injection contracts for account-scoped agent execution."""

from __future__ import annotations

from collections.abc import AsyncIterable, Awaitable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from webhub.chat.commands import SlashCommandInvocation

type AgentChunk = Mapping[str, Any]
type AgentChunkSource = Iterable[AgentChunk] | AsyncIterable[AgentChunk]
type AgentRunResult = AgentChunkSource | Awaitable[AgentChunkSource]


@dataclass(frozen=True, slots=True)
class AgentRunRequest:
    """Runner input with the authenticated account scope already attached.

    ``metadata`` contains bounded, JSON-compatible client hints.  It remains
    untrusted and must never override ``account_id`` or authorization policy.
    """

    account_id: str
    conversation_id: str | None
    message: str
    slash_command: SlashCommandInvocation | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def user_id(self) -> str:
        """Compatibility alias for adapters that call the owner a user."""

        return self.account_id


class AgentRunner(Protocol):
    """Provider/graph adapter supplied by the application composition layer."""

    def run(self, request: AgentRunRequest) -> AgentRunResult:
        """Return UI Message Stream chunks without performing account lookup."""


class AgentConversationAccess(Protocol):
    """Authorize an opaque conversation id against the authenticated account."""

    async def assert_owned(self, *, account_id: str, conversation_id: str) -> None:
        """Raise an ``AgentConversationUnavailableError`` on mismatch/missing."""


class AgentProviderNotConfiguredError(RuntimeError):
    """Raised when no usable account-level model Provider is configured."""

    code = "provider_not_configured"
    safe_message = "尚未配置可用的模型 Provider，请先在设置中完成配置。"


class AgentConversationUnavailableError(RuntimeError):
    """Raised when a conversation cannot be proven to belong to the account."""

    code = "conversation_unavailable"
    safe_message = "当前会话不可用，请新建对话后重试。"


class UnconfiguredAgentRunner:
    """Default runner: never calls a network or fabricates an answer."""

    async def run(self, request: AgentRunRequest) -> AgentChunkSource:
        del request
        raise AgentProviderNotConfiguredError


class RejectingConversationAccess:
    """Fail-closed placeholder until conversation persistence is wired."""

    async def assert_owned(self, *, account_id: str, conversation_id: str) -> None:
        del account_id, conversation_id
        raise AgentConversationUnavailableError


__all__ = [
    "AgentChunkSource",
    "AgentConversationAccess",
    "AgentConversationUnavailableError",
    "AgentProviderNotConfiguredError",
    "AgentRunRequest",
    "AgentRunner",
    "RejectingConversationAccess",
    "UnconfiguredAgentRunner",
]
