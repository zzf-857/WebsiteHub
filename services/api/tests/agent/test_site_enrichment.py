from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from webhub.agent import site_enrichment
from webhub.agent.provider_binding import ProviderBinding
from webhub.ingestion.enrichment import (
    MAX_PROVIDER_RETRY_AFTER_SECONDS,
    SiteCategoryOption,
    SiteEnrichmentRequest,
    SiteEnrichmentUnavailableError,
)


class _Session:
    async def rollback(self) -> None:
        return None


class _Database:
    @asynccontextmanager
    async def sessions(self):
        yield _Session()


def _request(*, bulk: bool = True) -> SiteEnrichmentRequest:
    return SiteEnrichmentRequest(
        user_id="user-1",
        site_id="site-1",
        expected_url="https://example.com/private-path?token=hidden",
        expected_version=1,
        hostname="example.com",
        final_hostname="example.com",
        site_name="Example",
        page_title="Example",
        meta_description="",
        page_text="This is sufficient public page evidence. " * 4,
        current_category_id="category-1",
        current_tag_ids=(),
        categories=(SiteCategoryOption(id="category-1", name="未分类", is_default=True),),
        existing_tags=(),
        bulk=bulk,
    )


def _binding() -> ProviderBinding:
    return ProviderBinding(
        kind="model",
        provider="openai",
        config_id="config-1",
        display_name="OpenAI",
        base_url="https://api.openai.com/v1",
        model_name="gpt-test",
        timeout_seconds=30,
        api_key="secret-key",
    )


def test_bulk_rate_limit_disables_sdk_retries_and_keeps_vendor_details_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "secret-vendor-body-and-url"
    retries: list[int] = []
    logged: list[str] = []

    class RateLimitError(RuntimeError):
        def __init__(self) -> None:
            self.response = SimpleNamespace(
                status_code=429,
                headers={"Retry-After": "120"},
                url=f"https://provider.example/{secret}",
            )
            super().__init__(secret)

    async def resolve(*_args: object, **_kwargs: object) -> ProviderBinding:
        return _binding()

    def build(_binding: ProviderBinding, *, max_retries: int = 1) -> object:
        retries.append(max_retries)
        return object()

    async def fail_graph(**_kwargs: object):
        raise RateLimitError

    def warning(message: str, *args: object, **_kwargs: object) -> None:
        logged.append(message % args)

    monkeypatch.setattr(site_enrichment, "resolve_binding", resolve)
    monkeypatch.setattr(site_enrichment, "build_chat_model", build)
    monkeypatch.setattr(site_enrichment, "_run_tool_graph", fail_graph)
    monkeypatch.setattr(site_enrichment._LOGGER, "warning", warning)

    enricher = site_enrichment.AgentSiteEnricher(_Database(), object())  # type: ignore[arg-type]
    with pytest.raises(SiteEnrichmentUnavailableError) as raised:
        asyncio.run(enricher.enrich(_request()))

    assert retries == [0]
    assert raised.value.safe_message == "模型未能完成网站资料分析"
    assert raised.value.failure_reason == "provider_rate_limited"
    assert raised.value.retry_after_seconds == 120
    assert raised.value.provider_failure is True
    assert raised.value.stop_batch is False
    assert all(secret not in entry for entry in logged)
    assert secret not in str(raised.value)


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        ("0", 1),
        ("0030", 30),
        ("999999999999999999999", MAX_PROVIDER_RETRY_AFTER_SECONDS),
        ("Wed, 21 Oct 2015 07:28:00 GMT", None),
        ("30 seconds", None),
        ("٣٠", None),
        (None, None),
    ),
)
def test_retry_after_accepts_only_bounded_ascii_delta_seconds(
    value: object,
    expected: int | None,
) -> None:
    error = RuntimeError("private vendor detail")
    error.response = SimpleNamespace(  # type: ignore[attr-defined]
        status_code=429,
        headers={"Retry-After": value},
    )

    policy = site_enrichment._provider_error_policy(error)

    assert policy.failure_reason == "provider_rate_limited"
    assert policy.retry_after_seconds == expected


def test_provider_failure_policy_separates_temporary_unavailable_and_internal() -> None:
    class AuthenticationError(RuntimeError):
        pass

    temporary = site_enrichment._provider_error_policy(TimeoutError("private timeout"))
    unavailable = site_enrichment._provider_error_policy(
        AuthenticationError("private authentication body")
    )
    internal = site_enrichment._provider_error_policy(ValueError("private local detail"))

    assert (
        temporary.stop_batch,
        temporary.provider_failure,
        temporary.failure_reason,
    ) == (False, True, "provider_temporary_failure")
    assert (
        unavailable.stop_batch,
        unavailable.provider_failure,
        unavailable.failure_reason,
    ) == (True, False, "provider_unavailable")
    assert (
        internal.stop_batch,
        internal.provider_failure,
        internal.failure_reason,
    ) == (True, False, "internal_error")
