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
    # Search vendors do not expose a common read-only catalogue endpoint.  A
    # supported search probe therefore performs one minimal real query and may
    # consume the account's search quota.  Keep this opt-in per adapter so a
    # newly registered vendor cannot accidentally advertise an unimplemented
    # or billable test.
    search_test_supported: bool = False
    # Free/shared search surfaces are suitable for low-frequency interactive
    # turns, but must never be selected by any multi-site/bulk analysis path.
    search_bulk_supported: bool = True
    usage_notice: str | None = None
    # Some shared, keyless services are intentionally pinned to one official
    # endpoint.  Accepting a caller-supplied URL for those adapters would make
    # the vendor label misleading and could send search queries to an
    # unexpected MCP server hidden behind the saved configuration.
    fixed_base_url: bool = False
    # Well-known origin for vendors with an official address.  A stored
    # ``base_url`` normally wins so users can opt into a proxy; ``fixed_base_url``
    # is the explicit exception.  ``None`` means the user must supply one (that
    # is exactly ``base_url_required``).
    default_base_url: str | None = None

    def supports(self, kind: ProviderKind) -> bool:
        return kind in self.kinds

    @property
    def connection_test_supported(self) -> bool:
        """Whether ``providers.connectivity`` can probe this vendor.

        Model and embedding vendors use a read-only catalogue.  Search-only
        vendors must explicitly opt into the minimal-query adapter above.
        """

        return bool(self.kinds & {"model", "embedding"}) or self.search_test_supported


# Keep this registry aligned with PRD 6.7 and Implementation Plan Phase 4.
PROVIDER_REGISTRY: tuple[ProviderDefinition, ...] = (
    ProviderDefinition(
        provider="openai",
        label="OpenAI",
        kinds=frozenset({"model", "embedding"}),
        secret_required=True,
        default_base_url="https://api.openai.com/v1",
    ),
    ProviderDefinition(
        provider="deepseek",
        label="DeepSeek",
        kinds=frozenset({"model"}),
        secret_required=True,
        default_base_url="https://api.deepseek.com/v1",
    ),
    ProviderDefinition(
        provider="qwen",
        label="通义千问",
        kinds=frozenset({"model", "embedding"}),
        secret_required=True,
        default_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    ),
    ProviderDefinition(
        provider="kimi",
        label="Kimi",
        kinds=frozenset({"model"}),
        secret_required=True,
        default_base_url="https://api.moonshot.cn/v1",
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
        search_test_supported=True,
        default_base_url="https://api.tavily.com",
    ),
    ProviderDefinition(
        provider="jina",
        label="Jina",
        kinds=frozenset({"search"}),
        secret_required=True,
        application_url="https://jina.ai/api-dashboard",
        search_test_supported=True,
        default_base_url="https://s.jina.ai",
    ),
    ProviderDefinition(
        provider="exa",
        label="Exa",
        kinds=frozenset({"search"}),
        secret_required=True,
        application_url="https://dashboard.exa.ai/api-keys",
        search_test_supported=True,
        default_base_url="https://api.exa.ai",
    ),
    ProviderDefinition(
        provider="exa_mcp_free",
        label="Exa MCP 免费额度",
        kinds=frozenset({"search"}),
        secret_required=False,
        search_bulk_supported=False,
        fixed_base_url=True,
        usage_notice=(
            "无需 API Key，使用 Exa 官方共享免费额度。仅适合低频 Agent 对话和"
            "单站手动分析；不用于批量回填。搜索词会发送给 Exa，达到共享限额后需"
            "改用自己的搜索 Provider。"
        ),
        default_base_url="https://mcp.exa.ai/mcp",
    ),
)

_BY_PROVIDER = {definition.provider: definition for definition in PROVIDER_REGISTRY}


def provider_definition(kind: ProviderKind, provider: str) -> ProviderDefinition:
    definition = _BY_PROVIDER.get(provider)
    if definition is None or not definition.supports(kind):
        raise ValueError("Provider 不存在或不支持该配置类型")
    return definition
