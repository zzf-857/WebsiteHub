"""Outbound web search through the account's enabled search Provider.

Web search remains opt-in at the account level.  The user can bring a Tavily /
Jina / Exa key, or explicitly enable the official low-frequency keyless Exa
MCP adapter; otherwise the Agent works from the library plus its own knowledge.
WebHub itself never ships or silently selects a vendor key.

Every adapter normalizes to the same ``WebSearchResult`` shape so the Agent
prompt and the UI never learn which vendor answered.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from webhub.bookmarks.models import FetchPolicy, NormalizationStatus
from webhub.bookmarks.normalization import normalize_bookmark_url
from webhub.bookmarks.privacy import sensitive_url_keys

from .provider_binding import ProviderBinding

MAX_RESULTS = 6
MAX_SNIPPET_LENGTH = 400
MAX_SEARCH_RESPONSE_BYTES = 512 * 1024
MAX_RESULT_URL_LENGTH = 2_048
_EXA_FREE_CACHE_SECONDS = 300
_EXA_FREE_CACHE_SIZE = 128
_EXA_FREE_COOLDOWN_SECONDS = 60
_EXA_FREE_MAX_TEXT_LENGTH = 100_000
_EXA_FREE_REQUEST_TIMEOUT_SECONDS = 15
_EXA_FREE_TOTAL_TIMEOUT_SECONDS = 16
_USER_AGENT = "WebHub/0.1 (+https://github.com/webhub)"

_EXA_FREE_CACHE: OrderedDict[
    tuple[str, str, str, int],
    tuple[float, tuple[WebSearchResult, ...]],
] = OrderedDict()
_EXA_FREE_SEMAPHORE: asyncio.Semaphore | None = None
_EXA_FREE_SEMAPHORE_LOOP: asyncio.AbstractEventLoop | None = None
_EXA_FREE_COOLDOWN_UNTIL = 0.0


class WebSearchUnavailableError(RuntimeError):
    """Raised when the configured search Provider could not be used.

    The message is intentionally generic: vendor errors routinely embed the
    request URL and fragments of the API key.
    """

    safe_message = "联网搜索暂时不可用，请稍后重试或检查搜索 Provider 配置。"


@dataclass(frozen=True, slots=True)
class WebSearchResult:
    title: str
    url: str
    snippet: str

    def as_dict(self) -> dict[str, str]:
        return {"title": self.title, "url": self.url, "snippet": self.snippet}


def _exa_free_semaphore() -> asyncio.Semaphore:
    """Keep the shared anonymous MCP allowance low-concurrency in this process."""

    global _EXA_FREE_SEMAPHORE, _EXA_FREE_SEMAPHORE_LOOP
    loop = asyncio.get_running_loop()
    if _EXA_FREE_SEMAPHORE is None or _EXA_FREE_SEMAPHORE_LOOP is not loop:
        _EXA_FREE_SEMAPHORE = asyncio.Semaphore(1)
        _EXA_FREE_SEMAPHORE_LOOP = loop
    return _EXA_FREE_SEMAPHORE


def _text(value: Any, *, limit: int = MAX_SNIPPET_LENGTH) -> str:
    if not isinstance(value, str):
        return ""
    collapsed = " ".join(value.split())
    return collapsed[:limit]


def _http_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if len(candidate) > MAX_RESULT_URL_LENGTH:
        return None
    if not candidate.lower().startswith(("http://", "https://")):
        return None
    return candidate


def trusted_source_url(value: Any) -> str | None:
    """Return a canonical, display-safe public source URL.

    This is deliberately stricter than the vendor result parser. Search hits
    remain useful to the model even when a URL is unsuitable for a clickable
    provenance part, but only public, credential-free and non-sensitive URLs
    are promoted to AI SDK ``source-url`` parts.
    """

    candidate = _http_url(value)
    if candidate is None or sensitive_url_keys(candidate):
        return None
    normalized = normalize_bookmark_url(candidate)
    if (
        normalized.status is not NormalizationStatus.ACCEPTED
        or normalized.normalized_url is None
        or normalized.fetch_policy is not FetchPolicy.PUBLIC_REVALIDATION_REQUIRED
    ):
        return None
    parts = urlsplit(normalized.normalized_url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def _collect(
    entries: Sequence[Any],
    *,
    title_keys: Sequence[str],
    snippet_keys: Sequence[str],
    limit: int,
) -> list[WebSearchResult]:
    results: list[WebSearchResult] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        url = _http_url(entry.get("url"))
        if url is None:
            continue
        title = next((_text(entry.get(key), limit=160) for key in title_keys if entry.get(key)), "")
        snippet = next((_text(entry.get(key)) for key in snippet_keys if entry.get(key)), "")
        results.append(WebSearchResult(title=title or url, url=url, snippet=snippet))
        if len(results) >= limit:
            break
    return results


def _parse_exa_mcp_text(value: str, *, limit: int) -> list[WebSearchResult]:
    """Parse the official Exa MCP tool's documented plain-text result blocks."""

    results: list[WebSearchResult] = []
    for block in value[:_EXA_FREE_MAX_TEXT_LENGTH].split("\n\n---\n\n"):
        title = ""
        url: str | None = None
        snippet_lines: list[str] = []
        reading_snippet = False
        for raw_line in block.splitlines():
            line = raw_line.strip()
            if not reading_snippet and line.startswith("Title:"):
                title = _text(line.removeprefix("Title:"), limit=160)
            elif not reading_snippet and line.startswith("URL:"):
                url = _http_url(line.removeprefix("URL:").strip())
            elif not reading_snippet and line.startswith(("Highlights:", "Text:")):
                reading_snippet = True
                remainder = line.partition(":")[2].strip()
                if remainder:
                    snippet_lines.append(remainder)
            elif reading_snippet and line:
                snippet_lines.append(line)
        if url is None:
            continue
        snippet = _text(" ".join(snippet_lines))
        results.append(WebSearchResult(title=title or url, url=url, snippet=snippet))
        if len(results) >= limit:
            break
    return results


async def _search_exa_free_mcp(
    binding: ProviderBinding,
    query: str,
    *,
    limit: int,
) -> list[WebSearchResult]:
    """Use Exa's official MCP with cache, cooldown and one process-local slot."""

    global _EXA_FREE_COOLDOWN_UNTIL

    # Include adapter identity and endpoint even though the keyless adapter is
    # currently pinned.  This keeps cache isolation correct if another fixed
    # MCP adapter is added later and protects old in-memory configurations.
    cache_key = (binding.provider, binding.base_url, query, limit)
    now = time.monotonic()
    cached = _EXA_FREE_CACHE.get(cache_key)
    if cached is not None and now - cached[0] <= _EXA_FREE_CACHE_SECONDS:
        _EXA_FREE_CACHE.move_to_end(cache_key)
        return list(cached[1])
    if now < _EXA_FREE_COOLDOWN_UNTIL:
        raise WebSearchUnavailableError("anonymous Exa MCP is cooling down")

    import httpx
    from mcp import ClientSession, types
    from mcp.client.streamable_http import streamable_http_client

    total_timeout_seconds = max(1, min(binding.timeout_seconds, _EXA_FREE_TOTAL_TIMEOUT_SECONDS))
    request_timeout_seconds = max(
        1,
        min(total_timeout_seconds, _EXA_FREE_REQUEST_TIMEOUT_SECONDS),
    )
    read_timeout = timedelta(seconds=request_timeout_seconds)
    provider_started = False
    try:
        # One wall-clock budget covers waiting for the shared slot, MCP
        # initialization, the tool call and context cleanup. Per-request HTTP
        # timeouts alone can each restart for every protocol round trip.
        async with asyncio.timeout(total_timeout_seconds), _exa_free_semaphore():
            # Re-check after waiting: another caller may have populated the cache or
            # tripped the shared cooldown while this request was queued.
            now = time.monotonic()
            cached = _EXA_FREE_CACHE.get(cache_key)
            if cached is not None and now - cached[0] <= _EXA_FREE_CACHE_SECONDS:
                _EXA_FREE_CACHE.move_to_end(cache_key)
                return list(cached[1])
            if now < _EXA_FREE_COOLDOWN_UNTIL:
                raise WebSearchUnavailableError("anonymous Exa MCP is cooling down")

            provider_started = True
            async with (
                httpx.AsyncClient(
                    timeout=request_timeout_seconds,
                    follow_redirects=False,
                    headers={"user-agent": _USER_AGENT},
                ) as http_client,
                streamable_http_client(
                    binding.base_url,
                    http_client=http_client,
                    # The SDK implements this as an additional DELETE request.
                    # Closing the ephemeral HTTP client already releases local
                    # resources; skip the optional DELETE so cleanup cannot
                    # consume a second full request timeout after a result.
                    terminate_on_close=False,
                ) as (read_stream, write_stream, _session_id),
                ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=read_timeout,
                ) as session,
            ):
                await session.initialize()
                response = await session.call_tool(
                    "web_search_exa",
                    arguments={"query": query, "numResults": limit},
                    read_timeout_seconds=read_timeout,
                )
    except asyncio.CancelledError:
        raise
    except WebSearchUnavailableError:
        raise
    except Exception as error:  # noqa: BLE001 - remote MCP errors stay private
        if provider_started:
            _EXA_FREE_COOLDOWN_UNTIL = time.monotonic() + _EXA_FREE_COOLDOWN_SECONDS
        raise WebSearchUnavailableError("anonymous Exa MCP request failed") from error

    if response.isError:
        _EXA_FREE_COOLDOWN_UNTIL = time.monotonic() + _EXA_FREE_COOLDOWN_SECONDS
        raise WebSearchUnavailableError("anonymous Exa MCP tool returned an error")
    text_parts = [
        item.text
        for item in response.content
        if isinstance(item, types.TextContent) and item.text
    ]
    results = _parse_exa_mcp_text("\n".join(text_parts), limit=limit)
    frozen = tuple(results)
    _EXA_FREE_CACHE[cache_key] = (time.monotonic(), frozen)
    _EXA_FREE_CACHE.move_to_end(cache_key)
    while len(_EXA_FREE_CACHE) > _EXA_FREE_CACHE_SIZE:
        _EXA_FREE_CACHE.popitem(last=False)
    return list(frozen)


async def search_web(
    binding: ProviderBinding,
    query: str,
    *,
    limit: int = MAX_RESULTS,
) -> list[WebSearchResult]:
    """Run one search against the account's configured search Provider."""

    normalized_query = " ".join(query.split())[:400]
    if not normalized_query:
        return []
    bounded_limit = max(1, min(limit, MAX_RESULTS))
    base_url = binding.base_url.rstrip("/")

    if binding.provider == "exa_mcp_free":
        return await _search_exa_free_mcp(
            binding,
            normalized_query,
            limit=bounded_limit,
        )

    import httpx

    if binding.provider == "tavily":
        request = {
            "method": "POST",
            "url": f"{base_url}/search",
            "headers": {"authorization": f"Bearer {binding.client_api_key}"},
            "json": {
                "query": normalized_query,
                "max_results": bounded_limit,
                "search_depth": "basic",
            },
        }
        payload_key, title_keys, snippet_keys = "results", ("title",), ("content", "raw_content")
    elif binding.provider == "exa":
        request = {
            "method": "POST",
            "url": f"{base_url}/search",
            "headers": {"x-api-key": binding.client_api_key},
            "json": {
                "query": normalized_query,
                "numResults": bounded_limit,
                "contents": {"text": {"maxCharacters": MAX_SNIPPET_LENGTH}},
            },
        }
        payload_key, title_keys, snippet_keys = "results", ("title",), ("text", "summary")
    elif binding.provider == "jina":
        request = {
            "method": "GET",
            "url": base_url,
            "headers": {
                "authorization": f"Bearer {binding.client_api_key}",
                "accept": "application/json",
            },
            "params": {"q": normalized_query},
        }
        payload_key, title_keys, snippet_keys = "data", ("title",), ("description", "content")
    else:
        raise WebSearchUnavailableError("unsupported search provider")

    headers = {**request.pop("headers"), "user-agent": _USER_AGENT}
    try:
        async with httpx.AsyncClient(
            timeout=binding.timeout_seconds,
            follow_redirects=False,
            headers=headers,
        ) as client:
            body = bytearray()
            async with client.stream(**request) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_SEARCH_RESPONSE_BYTES:
                        raise WebSearchUnavailableError("search provider response too large")
            payload = json.loads(bytes(body))
    except Exception as error:  # noqa: BLE001 - vendor errors must not escape
        raise WebSearchUnavailableError("search provider request failed") from error

    if not isinstance(payload, Mapping):
        return []
    entries = payload.get(payload_key)
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        return []
    return _collect(
        entries,
        title_keys=title_keys,
        snippet_keys=snippet_keys,
        limit=bounded_limit,
    )


__all__ = [
    "MAX_RESULTS",
    "MAX_SEARCH_RESPONSE_BYTES",
    "WebSearchResult",
    "WebSearchUnavailableError",
    "search_web",
    "trusted_source_url",
]
