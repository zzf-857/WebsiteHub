from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from webhub.agent.provider_binding import ProviderBinding
from webhub.agent.web_search import WebSearchUnavailableError, search_web


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

        async def request(self, **kwargs: Any) -> _FakeResponse:
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
