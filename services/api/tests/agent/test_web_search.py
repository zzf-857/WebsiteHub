from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from webhub.agent.provider_binding import ProviderBinding
from webhub.agent.web_search import (
    WebSearchUnavailableError,
    search_web,
    trusted_source_url,
)


def _binding(provider: str) -> ProviderBinding:
    return ProviderBinding(
        kind="search",
        provider=provider,
        config_id="config-1",
        display_name=provider,
        base_url={
            "tavily": "https://api.tavily.com",
            "exa": "https://api.exa.ai",
            "jina": "https://s.jina.ai",
        }[provider],
        model_name=None,
        timeout_seconds=5,
        api_key="search-key",
    )


class _FakeResponse:
    def __init__(self, payload: Any, *, status: int = 200) -> None:
        self._payload = payload
        self._status = status

    def raise_for_status(self) -> None:
        if self._status >= 400:
            raise httpx.HTTPError("vendor said no")

    def json(self) -> Any:
        return self._payload

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        yield json.dumps(self._payload).encode()


def _install_client(
    monkeypatch: pytest.MonkeyPatch,
    response: _FakeResponse | Exception,
) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def stream(self, **kwargs: Any) -> _FakeResponse:
            requests.append({**kwargs, "client": self.kwargs})
            if isinstance(response, Exception):
                raise response
            return response

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    return requests


def test_tavily_results_are_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = _install_client(
        monkeypatch,
        _FakeResponse(
            {
                "results": [
                    {
                        "title": "Qdrant",
                        "url": "https://qdrant.tech",
                        "content": "开源  向量\n检索引擎",
                    },
                    {"title": "无效条目", "url": "javascript:alert(1)"},
                    {"not": "a url"},
                ]
            }
        ),
    )

    results = asyncio.run(search_web(_binding("tavily"), "向量数据库"))

    assert [result.url for result in results] == ["https://qdrant.tech"]
    # Whitespace is collapsed so the model sees a compact snippet.
    assert results[0].snippet == "开源 向量 检索引擎"
    assert requests[0]["url"] == "https://api.tavily.com/search"
    assert requests[0]["json"]["query"] == "向量数据库"


def test_jina_and_exa_use_their_own_request_shapes(monkeypatch: pytest.MonkeyPatch) -> None:
    jina_requests = _install_client(
        monkeypatch,
        _FakeResponse({"data": [{"title": "A", "url": "https://a.example", "description": "d"}]}),
    )
    jina = asyncio.run(search_web(_binding("jina"), "关键词"))
    assert [result.title for result in jina] == ["A"]
    assert jina_requests[0]["method"] == "GET"
    assert jina_requests[0]["params"] == {"q": "关键词"}

    exa_requests = _install_client(
        monkeypatch,
        _FakeResponse({"results": [{"title": "B", "url": "https://b.example", "text": "t"}]}),
    )
    exa = asyncio.run(search_web(_binding("exa"), "关键词"))
    assert [result.title for result in exa] == ["B"]
    assert exa_requests[0]["client"]["headers"]["x-api-key"] == "search-key"


def test_vendor_failure_becomes_a_generic_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_client(monkeypatch, httpx.HTTPError("401 for https://api.tavily.com?key=search-key"))

    with pytest.raises(WebSearchUnavailableError) as failure:
        asyncio.run(search_web(_binding("tavily"), "关键词"))

    # Vendor text routinely embeds the endpoint and key fragments.
    assert "search-key" not in str(failure.value)
    assert "search-key" not in WebSearchUnavailableError.safe_message


def test_unexpected_payload_shapes_are_tolerated(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_client(monkeypatch, _FakeResponse({"results": "not-a-list"}))
    assert asyncio.run(search_web(_binding("tavily"), "关键词")) == []

    _install_client(monkeypatch, _FakeResponse(["unexpected"]))
    assert asyncio.run(search_web(_binding("tavily"), "关键词")) == []


def test_blank_query_never_reaches_the_vendor(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = _install_client(monkeypatch, _FakeResponse({"results": []}))

    assert asyncio.run(search_web(_binding("tavily"), "   ")) == []
    assert requests == []


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        (" https://Example.com/docs?q=rag#section ", "https://example.com/docs?q=rag"),
        ("https://example.com:443/docs", "https://example.com/docs"),
        ("http://example.com:80/docs", "http://example.com/docs"),
        ("javascript:alert(1)", None),
        ("http://[::1", None),
        (r"http://2130706433\example.com/", None),
        ("http://%31%32%37.0.0.1/", None),
        ("http://0x%37f000001/", None),
        ("http://intranet/admin", None),
        ("http://router/", None),
        ("http://service.corp/private", None),
        ("http://hidden.onion/private", None),
        ("http://fixture.example/private", None),
        ("https://home.arpa/admin", None),
        ("https://router.home.arpa/admin", None),
        ("https://example.com/\ud800", None),
        ("https://user:password@example.com/docs", None),
        ("https://example.com/docs?access_token=secret", None),
        ("http://127.0.0.1/private", None),
        ("http://2130706433/private", None),
        ("http://0x7f000001/private", None),
        ("http://0177.0.0.1/private", None),
        ("http://127.1/private", None),
        ("http://224.0.0.1/private", None),
        ("http://[ff02::1]/private", None),
    ],
)
def test_trusted_source_url_normalizes_public_urls_and_rejects_private_or_sensitive(
    candidate: str,
    expected: str | None,
) -> None:
    assert trusted_source_url(candidate) == expected
