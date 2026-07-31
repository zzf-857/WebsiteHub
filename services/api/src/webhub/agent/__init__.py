"""Account-scoped agent chat contracts.

The module intentionally does not register itself with the application.  The
application composition layer can include :data:`webhub.agent.routes.router`
once conversation persistence and a real provider runner are wired.
"""

from .access import DatabaseConversationAccess
from .routes import router
from .runner import (
    AgentChunkSource,
    AgentConversationAccess,
    AgentConversationUnavailableError,
    AgentProviderConfigurationInvalidError,
    AgentProviderCredentialsUnavailableError,
    AgentProviderError,
    AgentProviderFakeIPError,
    AgentProviderNotConfiguredError,
    AgentProviderTargetBlockedError,
    AgentProviderTargetUnavailableError,
    AgentRunner,
    AgentRunRequest,
    RejectingConversationAccess,
    UnconfiguredAgentRunner,
)
from .schemas import AgentChatRequest, SlashCommandMetadataInput

__all__ = [
    "AgentChatRequest",
    "AgentChunkSource",
    "AgentConversationAccess",
    "AgentConversationUnavailableError",
    "DatabaseConversationAccess",
    "AgentProviderConfigurationInvalidError",
    "AgentProviderCredentialsUnavailableError",
    "AgentProviderError",
    "AgentProviderFakeIPError",
    "AgentProviderNotConfiguredError",
    "AgentProviderTargetBlockedError",
    "AgentProviderTargetUnavailableError",
    "AgentRunRequest",
    "AgentRunner",
    "RejectingConversationAccess",
    "SlashCommandMetadataInput",
    "UnconfiguredAgentRunner",
    "router",
]
