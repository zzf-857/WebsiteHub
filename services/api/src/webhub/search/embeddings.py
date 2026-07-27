"""Turn text into vectors using the account's own embedding Provider.

Same posture as ``bookmarks.classifier``: this module declares the shape it
needs rather than importing ``agent.provider_binding``, because ``agent``
already imports downwards and closing that loop would create a cycle between
two top-level packages.

Every failure is non-fatal.  Semantic recall is an *enhancement* to keyword
search; when it is unavailable the product must fall back to FTS silently, not
show the user an error about a feature they may not know exists.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Protocol

_LOGGER = logging.getLogger(__name__)

# Vendors reject oversized batches; this also bounds one request's cost.
MAX_BATCH_INPUTS = 64
MAX_INPUT_CHARS = 8_000


class EmbeddingEndpoint(Protocol):
    """The four values needed from a resolved embedding Provider."""

    @property
    def base_url(self) -> str: ...

    @property
    def model_name(self) -> str | None: ...

    @property
    def timeout_seconds(self) -> int: ...

    @property
    def client_api_key(self) -> str: ...


async def embed_texts(
    binding: EmbeddingEndpoint,
    texts: Sequence[str],
) -> list[list[float]] | None:
    """Embed a batch, or return ``None`` when the Provider could not be used.

    ``None`` rather than an exception because every caller's correct response
    is the same — carry on with keyword search — and an exception would invite
    someone to surface vendor text the user should never see.
    """

    import httpx

    model = binding.model_name
    if not model or not texts:
        return None
    payload = {
        "model": model,
        "input": [text[:MAX_INPUT_CHARS] for text in texts[:MAX_BATCH_INPUTS]],
    }
    try:
        async with httpx.AsyncClient(
            timeout=binding.timeout_seconds,
            follow_redirects=False,
        ) as client:
            response = await client.post(
                f"{binding.base_url.rstrip('/')}/embeddings",
                headers={"authorization": f"Bearer {binding.client_api_key}"},
                json=payload,
            )
            if response.status_code != 200:
                # The body is not read: vendor errors echo the request and can
                # carry a prefix of the API key.
                _LOGGER.info("embedding provider returned %s", response.status_code)
                return None
            data = response.json()
    except Exception as error:  # noqa: BLE001 - vendor errors must not escape
        _LOGGER.info("embedding request failed", exc_info=error)
        return None

    try:
        items = data["data"]
        # Vendors are not guaranteed to preserve input order; `index` is.
        ordered = sorted(items, key=lambda item: int(item.get("index", 0)))
        vectors = [[float(value) for value in item["embedding"]] for item in ordered]
    except (KeyError, TypeError, ValueError) as error:
        _LOGGER.info("embedding response was not usable", exc_info=error)
        return None

    if not vectors or any(not vector for vector in vectors):
        return None
    # A batch that comes back a different length cannot be zipped with its
    # inputs; guessing the alignment would silently mislabel every vector.
    if len(vectors) != len(payload["input"]):
        _LOGGER.info("embedding response length did not match the request")
        return None
    return vectors


# 查询向量的进程内缓存。**这是一道花钱的闸门，不是性能优化。**
# 同一个查询词翻第二页要再嵌入一次、去抖打字连发三次要嵌入三次，
# 每一次都是用户账上的一笔真实请求，而结果逐字节相同。
# 键里含模型名：换模型后旧向量不可比较，必须重新嵌入。
_QUERY_CACHE: dict[tuple[str, str, str], list[float]] = {}
MAX_QUERY_CACHE = 256


async def embed_query(binding: EmbeddingEndpoint, query: str) -> list[float] | None:
    model = binding.model_name
    if not model:
        return None
    key = (binding.base_url, model, query)
    cached = _QUERY_CACHE.get(key)
    if cached is not None:
        return cached
    vectors = await embed_texts(binding, [query])
    vector = vectors[0] if vectors else None
    if vector is not None:
        # 朴素上限：满了就整个丢弃重来。做成 LRU 需要额外簿记，
        # 而这里的失效代价只是「下次重新嵌入一遍」，不值得。
        if len(_QUERY_CACHE) >= MAX_QUERY_CACHE:
            _QUERY_CACHE.clear()
        _QUERY_CACHE[key] = vector
    return vector


__all__ = [
    "MAX_BATCH_INPUTS",
    "MAX_QUERY_CACHE",
    "MAX_INPUT_CHARS",
    "EmbeddingEndpoint",
    "embed_query",
    "embed_texts",
]
