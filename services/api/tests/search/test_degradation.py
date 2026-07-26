from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx
import pytest

from webhub.search.embeddings import embed_query, embed_texts
from webhub.search.fusion import Candidate
from webhub.search.service import hybrid_search

_REAL_ASYNC_CLIENT = httpx.AsyncClient

VENDOR_LEAK = "invalid api key sk-live-abcd1234 at https://internal.vendor/v1"


@dataclass(frozen=True, slots=True)
class Endpoint:
    base_url: str = "https://api.example.com/v1"
    model_name: str | None = "embed-1"
    timeout_seconds: int = 5
    client_api_key: str = "sk-account-secret"


CANDIDATES = [Candidate("a", "站点 A"), Candidate("b", "站点 B")]


def _mock(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(record)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return seen


def _vectors(count: int) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "data": [
                {"index": index, "embedding": [1.0, 0.0]} for index in range(count)
            ]
        },
    )


def test_a_working_provider_returns_vectors_in_request_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        # Deliberately out of order: `index` is what guarantees alignment.
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.0, 1.0]},
                    {"index": 0, "embedding": [1.0, 0.0]},
                ]
            },
        )

    _mock(monkeypatch, handler)
    vectors = asyncio.run(embed_texts(Endpoint(), ["first", "second"]))
    assert vectors == [[1.0, 0.0], [0.0, 1.0]]


@pytest.mark.parametrize("status", [401, 429, 500])
def test_a_failing_provider_degrades_instead_of_raising(
    status: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Semantic recall is an enhancement; losing it must not break search."""

    _mock(monkeypatch, lambda _r: httpx.Response(status, json={"error": VENDOR_LEAK}))
    assert asyncio.run(embed_query(Endpoint(), "查询")) is None


def test_a_transport_failure_degrades_too(monkeypatch: pytest.MonkeyPatch) -> None:
    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    _mock(monkeypatch, refused)
    assert asyncio.run(embed_query(Endpoint(), "查询")) is None


def test_a_mismatched_batch_length_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guessing the alignment would silently mislabel every vector."""

    _mock(monkeypatch, lambda _r: _vectors(1))
    assert asyncio.run(embed_texts(Endpoint(), ["one", "two", "three"])) is None


def test_a_binding_without_a_model_never_calls_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _mock(monkeypatch, lambda _r: _vectors(1))
    assert asyncio.run(embed_query(Endpoint(model_name=None), "查询")) is None
    assert seen == []


def test_search_without_an_embedding_provider_still_ranks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The criterion: 未配置 embedding Provider 时自动降级到 FTS，不报错。"""

    seen = _mock(monkeypatch, lambda _r: _vectors(1))

    result = asyncio.run(
        hybrid_search(
            session=None,  # type: ignore[arg-type] - never touched without a binding
            user_id="alice",
            query="站点 A",
            keyword_ids=["b", "a"],
            candidates=CANDIDATES,
            binding=None,
        )
    )

    assert seen == []
    assert result.semantic_used is False
    # Keyword results still ranked, and the exact name still wins.
    assert [hit.site_id for hit in result.hits] == ["a", "b"]


def test_search_reports_semantic_as_unused_when_it_contributed_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty index looks the same as no Provider — say so rather than imply."""

    _mock(monkeypatch, lambda _r: httpx.Response(500, json={"error": VENDOR_LEAK}))

    result = asyncio.run(
        hybrid_search(
            session=None,  # type: ignore[arg-type]
            user_id="alice",
            query="站点 B",
            keyword_ids=["a", "b"],
            candidates=CANDIDATES,
            binding=Endpoint(),
        )
    )
    assert result.semantic_used is False
    assert [hit.site_id for hit in result.hits] == ["b", "a"]
