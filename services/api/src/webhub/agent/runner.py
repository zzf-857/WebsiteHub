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
    turn_id: str
    conversation_id: str | None
    message: str
    slash_command: SlashCommandInvocation | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    # Canonical raw request fields used only for idempotency. Routes can bind
    # protocol failures before a parsed SlashCommand exists.
    idempotency_payload: Mapping[str, Any] | None = None

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


_PROVIDER_ERROR_MESSAGES = {
    "provider_not_configured": "尚未配置可用的模型 Provider，请先在设置中完成配置。",
    "provider_configuration_invalid": (
        "模型 Provider 配置不完整或已失效，请检查服务商、Base URL、模型和密钥。"
    ),
    "provider_credentials_unavailable": (
        "已保存的 Provider 密钥当前无法解密，请检查 Provider 主密钥或重新保存 API Key。"
    ),
    "provider_fake_ip_detected": (
        "Provider 域名解析到了代理 Fake-IP，请在 Clash/Mihomo 的全局 fake-ip-filter 中"
        "排除该域名并重新应用配置。"
    ),
    "provider_target_blocked": (
        "Provider 地址解析到本机、私网或保留地址，已被安全策略阻止，请检查 Base URL 与 DNS。"
    ),
    "provider_target_unavailable": (
        "Provider 地址暂时无法解析或解析超时，请检查 DNS、代理和网络后重试。"
    ),
}


class AgentProviderError(RuntimeError):
    """A Provider preflight failure with a fixed, safe client contract."""

    code = "provider_unavailable"
    safe_message = "模型 Provider 当前不可用，请检查配置后重试。"

    def __init__(self, *_: object) -> None:
        # Never retain caller-provided text: an exception cause may contain a
        # URL, response body, or credential fragment.
        super().__init__(self.code)


class AgentProviderNotConfiguredError(AgentProviderError):
    """Raised when no enabled account-level model Provider exists."""

    code = "provider_not_configured"
    safe_message = _PROVIDER_ERROR_MESSAGES[code]


class AgentProviderConfigurationInvalidError(AgentProviderError):
    """Raised when an enabled Provider row is incomplete or unsupported."""

    code = "provider_configuration_invalid"
    safe_message = _PROVIDER_ERROR_MESSAGES[code]


class AgentProviderCredentialsUnavailableError(AgentProviderError):
    """Raised when a stored Provider secret is missing or cannot be decrypted."""

    code = "provider_credentials_unavailable"
    safe_message = _PROVIDER_ERROR_MESSAGES[code]


class AgentProviderFakeIPError(AgentProviderError):
    """Raised when proxy DNS returns an RFC 2544 benchmarking address."""

    code = "provider_fake_ip_detected"
    safe_message = _PROVIDER_ERROR_MESSAGES[code]


class AgentProviderTargetBlockedError(AgentProviderError):
    """Raised when SSRF policy rejects a resolved Provider target."""

    code = "provider_target_blocked"
    safe_message = _PROVIDER_ERROR_MESSAGES[code]


class AgentProviderTargetUnavailableError(AgentProviderError):
    """Raised when Provider DNS resolution times out or fails."""

    code = "provider_target_unavailable"
    safe_message = _PROVIDER_ERROR_MESSAGES[code]


def agent_provider_error_message(code: str | None) -> str | None:
    """Return a fixed safe message for a persisted Provider error code."""

    return _PROVIDER_ERROR_MESSAGES.get(code or "")


class AgentConversationUnavailableError(RuntimeError):
    """Raised when a conversation cannot be proven to belong to the account."""

    code = "conversation_unavailable"
    safe_message = "当前会话不可用，请新建对话后重试。"


class AgentRunnerExecutionError(RuntimeError):
    """Sanitized runner failure carrying only a coarse stage and exception type."""

    def __init__(self, *, stage: str, error_type: str) -> None:
        self.stage = stage
        self.error_type = error_type
        super().__init__("Agent runner execution failed")


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
    "AgentProviderConfigurationInvalidError",
    "AgentProviderCredentialsUnavailableError",
    "AgentProviderError",
    "AgentProviderFakeIPError",
    "AgentProviderNotConfiguredError",
    "AgentProviderTargetBlockedError",
    "AgentProviderTargetUnavailableError",
    "AgentRunnerExecutionError",
    "AgentRunRequest",
    "AgentRunner",
    "RejectingConversationAccess",
    "UnconfiguredAgentRunner",
    "agent_provider_error_message",
]
