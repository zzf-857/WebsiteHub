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
        json={"data": [{"index": index, "embedding": [1.0, 0.0]} for index in range(count)]},
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


def test_the_same_query_is_only_embedded_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Paginating a search must not re-buy the same query vector.

    Without the cache, ``_relevance_page`` embeds on every page: four pages of
    one search is four billable requests for a byte-identical result.
    """

    from webhub.search import embeddings

    embeddings._QUERY_CACHE.clear()
    seen = _mock(monkeypatch, lambda _r: _vectors(1))

    async def scenario() -> tuple[list[float] | None, list[float] | None, int]:
        first = await embed_query(Endpoint(), "同一个查询词")
        second = await embed_query(Endpoint(), "同一个查询词")
        return first, second, len(seen)

    first, second, calls = asyncio.run(scenario())
    embeddings._QUERY_CACHE.clear()

    assert first == second
    assert calls == 1, "重复查询同一个词不得重复消耗额度"


def test_a_different_model_is_not_served_from_the_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vectors from two models are incomparable — the cache key must separate them."""

    from webhub.search import embeddings

    embeddings._QUERY_CACHE.clear()
    seen = _mock(monkeypatch, lambda _r: _vectors(1))

    async def scenario() -> int:
        await embed_query(Endpoint(model_name="embed-1"), "查询")
        await embed_query(Endpoint(model_name="embed-2"), "查询")
        return len(seen)

    calls = asyncio.run(scenario())
    embeddings._QUERY_CACHE.clear()
    assert calls == 2


def test_a_broken_vector_store_still_returns_keyword_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`nearest` hits the local DB — its errors must not take down search.

    `embed_texts` already collapses vendor failures, but a SQLAlchemyError from
    the vector query would otherwise escape to the route and 500 the request,
    turning an enhancement into a single point of failure.
    """

    from sqlalchemy.exc import SQLAlchemyError

    from webhub.search import embeddings
    from webhub.search import service as search_service

    embeddings._QUERY_CACHE.clear()
    _mock(monkeypatch, lambda _r: _vectors(1))

    async def exploding_nearest(*_args: object, **_kwargs: object) -> list[object]:
        raise SQLAlchemyError("vector table is gone")

    monkeypatch.setattr(search_service, "nearest", exploding_nearest)

    result = asyncio.run(
        hybrid_search(
            session=None,  # type: ignore[arg-type]
            user_id="alice",
            query="站点 A",
            keyword_ids=["b", "a"],
            candidates=CANDIDATES,
            binding=Endpoint(),
        )
    )
    embeddings._QUERY_CACHE.clear()

    assert result.semantic_used is False
    assert [hit.site_id for hit in result.hits] == ["a", "b"], "关键词结果必须完好"
