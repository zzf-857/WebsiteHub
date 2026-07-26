"""Outbound web search through the account's own search Provider.

The todolist deliberately leaves web search as an opt-in capability: the user
brings a Tavily / Jina / Exa key, or the Agent simply works from the library
plus its own knowledge.  Nothing here has a fallback vendor key.

Every adapter normalizes to the same ``WebSearchResult`` shape so the Agent
prompt and the UI never learn which vendor answered.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .provider_binding import ProviderBinding

MAX_RESULTS = 6
MAX_SNIPPET_LENGTH = 400
_USER_AGENT = "WebHub/0.1 (+https://github.com/webhub)"


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


def _text(value: Any, *, limit: int = MAX_SNIPPET_LENGTH) -> str:
    if not isinstance(value, str):
        return ""
    collapsed = " ".join(value.split())
    return collapsed[:limit]


def _http_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate.lower().startswith(("http://", "https://")):
        return None
    return candidate


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


async def search_web(
    binding: ProviderBinding,
    query: str,
    *,
    limit: int = MAX_RESULTS,
) -> list[WebSearchResult]:
    """Run one search against the account's configured search Provider."""

    import httpx

    normalized_query = " ".join(query.split())[:400]
    if not normalized_query:
        return []
    bounded_limit = max(1, min(limit, MAX_RESULTS))
    base_url = binding.base_url.rstrip("/")

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
            response = await client.request(**request)
            response.raise_for_status()
            payload = response.json()
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
    "WebSearchResult",
    "WebSearchUnavailableError",
    "search_web",
]
