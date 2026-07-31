from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from ipaddress import ip_address

import httpx
import pytest

from webhub.providers import targets
from webhub.providers.connectivity import (
    MAX_RESPONSE_BYTES,
    ProviderProbeError,
    probe_base_url,
    probe_models,
    probe_search,
)
from webhub.providers.registry import provider_definition

OPENAI = provider_definition("model", "openai")
OLLAMA = provider_definition("model", "ollama")
COMPATIBLE = provider_definition("model", "openai_compatible")
TAVILY = provider_definition("search", "tavily")
JINA = provider_definition("search", "jina")
EXA = provider_definition("search", "exa")
EXA_FREE = provider_definition("search", "exa_mcp_free")

# Vendor error bodies routinely echo the request URL, the request body and a
# prefix of the API key.  Every failure test asserts this string never surfaces.
VENDOR_LEAK = "Incorrect API key provided: sk-live-abcd1234 at https://internal.vendor/x"

# Captured once at import: a test that calls _run twice must not end up wrapping
# the factory installed by its own earlier call.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _run(
    definition,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    base_url: str,
    api_key: str | None = "sk-probe-secret",
    monkeypatch: pytest.MonkeyPatch,
    skip_target_check: bool = True,
) -> tuple[list[httpx.Request], object]:
    """Drive ``probe_models`` against a mock transport and capture the requests."""

    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def client_factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(record)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)

    if skip_target_check:
        # SSRF validation does real DNS; the HTTP-behaviour tests stub it out and
        # the dedicated SSRF tests below exercise it for real with IP literals.
        async def allow(*_args: object, **_kwargs: object) -> None:
            return None

        monkeypatch.setattr(
            "webhub.providers.connectivity.validate_connection_target",
            allow,
        )

    outcome: object
    try:
        outcome = asyncio.run(
            probe_models(
                definition,
                base_url=base_url,
                api_key=api_key,
                timeout_seconds=2,
            )
        )
    except ProviderProbeError as error:
        outcome = error
    return seen, outcome


def _run_search(
    definition,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    base_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[httpx.Request], object]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def client_factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(record)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)  # type: ignore[arg-type]

    async def allow(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    monkeypatch.setattr("webhub.providers.connectivity.validate_connection_target", allow)
    try:
        outcome: object = asyncio.run(
            probe_search(
                definition,
                base_url=base_url,
                api_key="search-probe-key",
                timeout_seconds=2,
            )
        )
    except ProviderProbeError as error:
        outcome = error
    return seen, outcome


def _json(payload: object, status: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _request: httpx.Response(status, json=payload)


def test_openai_compatible_catalogue_is_deduped_sorted_and_authenticated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "data": [
            {"id": "gpt-4o-mini"},
            {"id": "gpt-4o"},
            {"id": "gpt-4o"},
            {"id": "  "},
            {"name": "text-embedding-3-small"},
            "not-a-mapping-but-a-string",
            {"unexpected": "shape"},
        ]
    }
    seen, outcome = _run(
        OPENAI,
        _json(payload),
        base_url="https://api.openai.com/v1",
        monkeypatch=monkeypatch,
    )

    assert not isinstance(outcome, ProviderProbeError)
    assert outcome.models == [
        "gpt-4o",
        "gpt-4o-mini",
        "not-a-mapping-but-a-string",
        "text-embedding-3-small",
    ]
    assert len(seen) == 1
    assert seen[0].method == "GET"
    assert str(seen[0].url) == "https://api.openai.com/v1/models"
    assert seen[0].headers["authorization"] == "Bearer sk-probe-secret"


def test_ollama_uses_native_tags_endpoint_and_sends_no_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {"models": [{"name": "qwen3:8b"}, {"name": "llama3.2:3b"}]}
    seen, outcome = _run(
        OLLAMA,
        _json(payload),
        base_url="http://127.0.0.1:11434/v1",
        api_key=None,
        monkeypatch=monkeypatch,
    )

    assert not isinstance(outcome, ProviderProbeError)
    assert outcome.models == ["llama3.2:3b", "qwen3:8b"]
    # The stored base URL may carry the OpenAI-compatible /v1 suffix; the native
    # catalogue lives at the root.
    assert str(seen[0].url) == "http://127.0.0.1:11434/api/tags"
    assert "authorization" not in seen[0].headers


def test_empty_catalogue_is_a_success_not_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _, outcome = _run(
        OPENAI,
        _json({"data": []}),
        base_url="https://api.openai.com/v1",
        monkeypatch=monkeypatch,
    )
    assert not isinstance(outcome, ProviderProbeError)
    assert outcome.models == []


def test_search_probes_use_each_vendor_contract_and_only_return_a_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tavily_seen, tavily = _run_search(
        TAVILY,
        _json({"results": [{"title": "private vendor text"}]}),
        base_url="https://api.tavily.com",
        monkeypatch=monkeypatch,
    )
    assert not isinstance(tavily, ProviderProbeError)
    assert tavily.result_count == 1
    assert tavily_seen[0].method == "POST"
    assert str(tavily_seen[0].url) == "https://api.tavily.com/search"
    assert tavily_seen[0].headers["authorization"] == "Bearer search-probe-key"
    assert json.loads(tavily_seen[0].content)["max_results"] == 1

    exa_seen, exa = _run_search(
        EXA,
        _json({"results": []}),
        base_url="https://api.exa.ai",
        monkeypatch=monkeypatch,
    )
    assert not isinstance(exa, ProviderProbeError)
    assert exa.result_count == 0
    assert exa_seen[0].method == "POST"
    assert str(exa_seen[0].url) == "https://api.exa.ai/search"
    assert exa_seen[0].headers["x-api-key"] == "search-probe-key"
    assert json.loads(exa_seen[0].content)["numResults"] == 1

    jina_seen, jina = _run_search(
        JINA,
        _json({"data": [{"url": "https://example.com"}]}),
        base_url="https://s.jina.ai",
        monkeypatch=monkeypatch,
    )
    assert not isinstance(jina, ProviderProbeError)
    assert jina.result_count == 1
    assert jina_seen[0].method == "GET"
    assert str(jina_seen[0].url).startswith("https://s.jina.ai?q=")
    assert jina_seen[0].url.params["q"] == "WebHub connectivity test"
    assert jina_seen[0].headers["authorization"] == "Bearer search-probe-key"


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "provider_auth_failed"),
        (403, "provider_auth_failed"),
        (404, "provider_endpoint_not_found"),
        (429, "provider_rate_limited"),
        (400, "provider_response_invalid"),
        (500, "provider_upstream_error"),
        (503, "provider_upstream_error"),
        (302, "provider_redirected"),
    ],
)
def test_error_statuses_map_to_fixed_messages_without_vendor_text(
    status: int,
    code: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            json={"error": {"message": VENDOR_LEAK}},
            headers={"location": "http://169.254.169.254/latest/meta-data/"},
        )

    seen, outcome = _run(
        OPENAI,
        handler,
        base_url="https://api.openai.com/v1",
        monkeypatch=monkeypatch,
    )

    assert isinstance(outcome, ProviderProbeError)
    assert outcome.code == code
    assert VENDOR_LEAK not in outcome.message
    assert "sk-probe-secret" not in outcome.message
    assert outcome.message.strip()
    # A redirect must never be followed: exactly one request left the process.
    assert len(seen) == 1


def test_timeout_and_transport_failures_are_distinguished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timeout(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=_request)

    _, timed_out = _run(
        OPENAI,
        timeout,
        base_url="https://api.openai.com/v1",
        monkeypatch=monkeypatch,
    )
    assert isinstance(timed_out, ProviderProbeError)
    assert timed_out.code == "provider_timeout"

    def refused(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=_request)

    _, unreachable = _run(
        OPENAI,
        refused,
        base_url="https://api.openai.com/v1",
        monkeypatch=monkeypatch,
    )
    assert isinstance(unreachable, ProviderProbeError)
    assert unreachable.code == "provider_unreachable"


@pytest.mark.parametrize(
    "body",
    [b"not json at all", b'{"data": "a string, not a list"}', b"[]", b'{"models": []}'],
)
def test_unparseable_or_unexpected_payloads_fail_cleanly(
    body: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, outcome = _run(
        OPENAI,
        lambda _request: httpx.Response(200, content=body),
        base_url="https://api.openai.com/v1",
        monkeypatch=monkeypatch,
    )
    assert isinstance(outcome, ProviderProbeError)
    assert outcome.code == "provider_response_invalid"


def test_oversized_body_is_aborted(monkeypatch: pytest.MonkeyPatch) -> None:
    oversized = json.dumps({"data": [{"id": "m" * 200}] * 20_000}).encode()
    assert len(oversized) > MAX_RESPONSE_BYTES

    _, outcome = _run(
        OPENAI,
        lambda _request: httpx.Response(200, content=oversized),
        base_url="https://api.openai.com/v1",
        monkeypatch=monkeypatch,
    )
    assert isinstance(outcome, ProviderProbeError)
    assert outcome.code == "provider_response_too_large"


@pytest.mark.parametrize(
    "base_url",
    [
        "https://127.0.0.1/v1",
        "https://10.1.2.3/v1",
        "https://192.168.0.4/v1",
        "https://169.254.169.254/v1",
        "https://[::1]/v1",
    ],
)
def test_private_targets_are_refused_before_any_request_leaves(
    base_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen, outcome = _run(
        COMPATIBLE,
        _json({"data": []}),
        base_url=base_url,
        monkeypatch=monkeypatch,
        skip_target_check=False,
    )
    assert isinstance(outcome, ProviderProbeError)
    assert outcome.code == "unsafe_provider_target"
    # The SSRF check runs first; nothing was sent.
    assert seen == []


def test_proxy_fake_ip_gets_a_precise_probe_error_before_outbound_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        targets,
        "_resolve",
        lambda _hostname, _port: {ip_address("198.18.0.42")},
    )

    seen, outcome = _run(
        COMPATIBLE,
        _json({"data": []}),
        base_url="https://provider.example/v1",
        monkeypatch=monkeypatch,
        skip_target_check=False,
    )

    assert isinstance(outcome, ProviderProbeError)
    assert outcome.code == "provider_fake_ip_detected"
    assert "Clash/Mihomo" in outcome.message
    assert seen == []


def test_ollama_may_reach_loopback_that_other_vendors_may_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen, outcome = _run(
        OLLAMA,
        _json({"models": [{"name": "qwen3:8b"}]}),
        base_url="http://127.0.0.1:11434",
        api_key=None,
        monkeypatch=monkeypatch,
        skip_target_check=False,
    )
    assert not isinstance(outcome, ProviderProbeError)
    assert outcome.models == ["qwen3:8b"]
    assert len(seen) == 1


def test_probe_base_url_falls_back_to_the_registry_default() -> None:
    assert probe_base_url(OPENAI, None) == "https://api.openai.com/v1"
    assert probe_base_url(OPENAI, "  ") == "https://api.openai.com/v1"
    # A stored value always wins, and its trailing slash is dropped.
    assert probe_base_url(OPENAI, "https://proxy.example.com/v1/") == "https://proxy.example.com/v1"
    # The keyless adapter is pinned to its audited endpoint, including for
    # legacy rows that once stored a caller-supplied address.
    assert probe_base_url(
        EXA_FREE,
        "https://attacker.example/mcp",
    ) == "https://mcp.exa.ai/mcp"
    # Vendors with no default and no stored value have nowhere to go.
    with pytest.raises(ProviderProbeError) as raised:
        probe_base_url(COMPATIBLE, None)
    assert raised.value.code == "provider_unreachable"


def test_registered_probe_adapters_advertise_a_connection_test() -> None:
    assert OPENAI.connection_test_supported is True
    assert OLLAMA.connection_test_supported is True
    assert COMPATIBLE.connection_test_supported is True
    # Search vendors opt into an explicit minimal-query adapter.
    assert TAVILY.connection_test_supported is True
    assert JINA.connection_test_supported is True
    assert EXA.connection_test_supported is True
