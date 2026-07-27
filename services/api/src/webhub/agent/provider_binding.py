"""Resolve an account's enabled Provider config into a usable client binding.

WebHub never ships a built-in vendor key: every model, embedding and search
call runs on credentials the account owner stored themselves.  This module is
the only place where such a credential is decrypted for outbound use.

Three invariants hold here and must keep holding:

* the plaintext secret lives on a ``repr=False`` field, so it cannot leak into
  a traceback, log line, or ``dataclasses.asdict`` dump;
* the base URL is re-validated against the SSRF rules right before use, not
  just when the config was saved (DNS can be re-pointed in between);
* every failure collapses into ``AgentProviderNotConfiguredError`` so the
  caller can never accidentally forward vendor error text to the browser.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.config import Settings
from webhub.db.models import ProviderConfig
from webhub.providers.registry import (
    PROVIDER_REGISTRY,
    ProviderDefinition,
    ProviderKind,
    provider_definition,
)
from webhub.providers.security import (
    ProviderSecretInvalidError,
    ProviderSecretUnavailableError,
    decrypt_secret,
)
from webhub.providers.targets import ProviderTargetError, validate_connection_target

from .runner import AgentProviderNotConfiguredError

# Vendors that expose their API at a well-known origin.  A stored ``base_url``
# always wins; these only spare the user from typing a URL they cannot
# meaningfully choose.  Derived from the registry so the connection probe and
# the Agent runtime can never disagree about where a vendor lives.
DEFAULT_BASE_URLS: dict[str, str] = {
    definition.provider: definition.default_base_url
    for definition in PROVIDER_REGISTRY
    if definition.default_base_url is not None
}

# Ollama speaks its own protocol at the root and an OpenAI-compatible one under
# ``/v1``; users normally save the root.
_OPENAI_COMPATIBLE_SUFFIX = "/v1"
_PLACEHOLDER_KEY = "webhub-no-key-required"


@dataclass(frozen=True, slots=True)
class ProviderBinding:
    """One resolved, ready-to-call Provider for a single account."""

    kind: ProviderKind
    provider: str
    config_id: str
    display_name: str
    base_url: str
    model_name: str | None
    timeout_seconds: int
    api_key: str | None = field(default=None, repr=False)

    @property
    def client_api_key(self) -> str:
        """Return a non-empty key: OpenAI SDK clients reject ``None``."""

        return self.api_key or _PLACEHOLDER_KEY


def _resolved_base_url(
    definition: ProviderDefinition,
    stored_base_url: str | None,
) -> str:
    candidate = (stored_base_url or "").strip().rstrip("/")
    if not candidate:
        candidate = DEFAULT_BASE_URLS.get(definition.provider, "")
    if not candidate:
        # base_url_required providers (ollama, openai_compatible) have no
        # sensible default; an incomplete config is a configuration error.
        raise AgentProviderNotConfiguredError
    if definition.provider == "ollama" and not candidate.endswith(_OPENAI_COMPATIBLE_SUFFIX):
        candidate = f"{candidate}{_OPENAI_COMPATIBLE_SUFFIX}"
    return candidate


async def load_enabled_config(
    session: AsyncSession,
    user_id: str,
    kind: ProviderKind,
) -> ProviderConfig | None:
    """Return the account's single enabled config for ``kind``, if any.

    ``uq_provider_configs_enabled_per_user_kind`` guarantees at most one row,
    so no tie-breaking is needed here.
    """

    return await session.scalar(
        select(ProviderConfig).where(
            ProviderConfig.user_id == user_id,
            ProviderConfig.kind == kind,
            ProviderConfig.enabled.is_(True),
        )
    )


async def resolve_binding(
    session: AsyncSession,
    settings: Settings,
    *,
    user_id: str,
    kind: ProviderKind,
) -> ProviderBinding:
    """Decrypt and validate the account's enabled Provider for ``kind``.

    Raises ``AgentProviderNotConfiguredError`` for every failure mode: absent,
    incomplete, undecryptable, or pointing at an unsafe network target.
    """

    config = await load_enabled_config(session, user_id, kind)
    if config is None:
        raise AgentProviderNotConfiguredError
    if config.user_id != user_id:  # defence in depth against a widened query
        raise AgentProviderNotConfiguredError

    try:
        definition = provider_definition(cast(ProviderKind, config.kind), config.provider)
    except ValueError as error:
        raise AgentProviderNotConfiguredError from error

    model_name = (config.model_name or "").strip() or None
    if kind in {"model", "embedding"} and model_name is None:
        raise AgentProviderNotConfiguredError

    has_secret = config.secret_ciphertext is not None and config.secret_nonce is not None
    api_key: str | None = None
    if has_secret:
        try:
            api_key = decrypt_secret(
                settings,
                config.secret_ciphertext or b"",
                config.secret_nonce or b"",
                config.key_version,
                user_id=user_id,
                config_id=config.id,
                kind=config.kind,
                provider=config.provider,
            )
        except (ProviderSecretUnavailableError, ProviderSecretInvalidError) as error:
            raise AgentProviderNotConfiguredError from error
    elif definition.secret_required:
        raise AgentProviderNotConfiguredError

    base_url = _resolved_base_url(definition, config.base_url)
    try:
        # Re-resolve now: a hostname that was safe at save time can be
        # re-pointed at a private address before this call.
        await validate_connection_target(
            base_url,
            allow_private=definition.allows_private_base_url,
            timeout_seconds=settings.provider_test_timeout_seconds,
        )
    except ProviderTargetError as error:
        raise AgentProviderNotConfiguredError from error

    return ProviderBinding(
        kind=kind,
        provider=config.provider,
        config_id=config.id,
        display_name=config.display_name,
        base_url=base_url,
        model_name=model_name,
        timeout_seconds=settings.agent_request_timeout_seconds,
        api_key=api_key,
    )


async def resolve_optional_binding(
    session: AsyncSession,
    settings: Settings,
    *,
    user_id: str,
    kind: ProviderKind,
) -> ProviderBinding | None:
    """Resolve ``kind`` but treat "not configured" as an absent capability.

    Used for search and embeddings, which enrich a turn rather than gate it.
    """

    try:
        return await resolve_binding(session, settings, user_id=user_id, kind=kind)
    except AgentProviderNotConfiguredError:
        return None


def build_chat_model(binding: ProviderBinding):  # noqa: ANN201 - langchain type
    """Build a streaming chat client for an OpenAI-compatible endpoint.

    Imported lazily so that importing ``webhub.agent`` stays cheap for the
    routes and tests that never construct a model.
    """

    from .openai_compatible import ReasoningCompatibleChatOpenAI

    if binding.model_name is None:
        raise AgentProviderNotConfiguredError
    return ReasoningCompatibleChatOpenAI(
        model=binding.model_name,
        api_key=binding.client_api_key,
        base_url=binding.base_url,
        timeout=binding.timeout_seconds,
        max_retries=1,
        streaming=True,
        # These first-party endpoints implement the standard
        # stream_options.include_usage contract.  Generic compatible endpoints
        # are intentionally left off: some reject that option outright.
        stream_usage=binding.provider in {"openai", "deepseek"},
    )


__all__ = [
    "DEFAULT_BASE_URLS",
    "ProviderBinding",
    "build_chat_model",
    "load_enabled_config",
    "resolve_binding",
    "resolve_optional_binding",
]
