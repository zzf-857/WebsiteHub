from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ProviderKind = Literal["model", "search", "embedding"]


@dataclass(frozen=True, slots=True)
class ProviderDefinition:
    provider: str
    label: str
    kinds: frozenset[ProviderKind]
    secret_required: bool
    base_url_required: bool = False
    allows_private_base_url: bool = False
    application_url: str | None = None

    def supports(self, kind: ProviderKind) -> bool:
        return kind in self.kinds


# Keep this registry aligned with PRD 6.7 and Implementation Plan Phase 4.
PROVIDER_REGISTRY: tuple[ProviderDefinition, ...] = (
    ProviderDefinition(
        provider="openai",
        label="OpenAI",
        kinds=frozenset({"model", "embedding"}),
        secret_required=True,
    ),
    ProviderDefinition(
        provider="deepseek",
        label="DeepSeek",
        kinds=frozenset({"model"}),
        secret_required=True,
    ),
    ProviderDefinition(
        provider="qwen",
        label="通义千问",
        kinds=frozenset({"model", "embedding"}),
        secret_required=True,
    ),
    ProviderDefinition(
        provider="kimi",
        label="Kimi",
        kinds=frozenset({"model"}),
        secret_required=True,
    ),
    ProviderDefinition(
        provider="ollama",
        label="Ollama",
        kinds=frozenset({"model", "embedding"}),
        secret_required=False,
        base_url_required=True,
        allows_private_base_url=True,
    ),
    ProviderDefinition(
        provider="openai_compatible",
        label="OpenAI-compatible",
        kinds=frozenset({"model", "embedding"}),
        secret_required=True,
        base_url_required=True,
    ),
    ProviderDefinition(
        provider="tavily",
        label="Tavily",
        kinds=frozenset({"search"}),
        secret_required=True,
        application_url="https://app.tavily.com/home",
    ),
    ProviderDefinition(
        provider="jina",
        label="Jina",
        kinds=frozenset({"search"}),
        secret_required=True,
        application_url="https://jina.ai/api-dashboard",
    ),
    ProviderDefinition(
        provider="exa",
        label="Exa",
        kinds=frozenset({"search"}),
        secret_required=True,
        application_url="https://dashboard.exa.ai/api-keys",
    ),
)

_BY_PROVIDER = {definition.provider: definition for definition in PROVIDER_REGISTRY}


def provider_definition(kind: ProviderKind, provider: str) -> ProviderDefinition:
    definition = _BY_PROVIDER.get(provider)
    if definition is None or not definition.supports(kind):
        raise ValueError("Provider 不存在或不支持该配置类型")
    return definition
