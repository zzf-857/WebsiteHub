"""One conditional GET of a feed through the existing safe outbound channel.

``ingestion.fetcher.pinned_public_stream`` resolves and SSRF-checks the target,
pins the connection to the approved address and never follows redirects on its
own; this module follows at most ``MAX_REDIRECTS`` hops, each re-validated by a
fresh pinned stream.  The body is streamed and capped.  Failures are returned
as a short code, never raised, and vendor text never leaves this module.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from urllib.parse import urljoin, urlsplit

from webhub.ingestion.fetcher import _pinned_stream, pinned_public_stream
from webhub.providers.targets import ProviderTargetError

MAX_REDIRECTS = 3
MAX_FEED_BYTES = 2 * 1024 * 1024
FETCH_TIMEOUT_SECONDS = 10
_USER_AGENT = "WebHub-Feeds/0.1 (+https://github.com/webhub)"
_ACCEPT = (
    "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.9, */*;q=0.5"
)

if TYPE_CHECKING:
    import httpx

TRUSTED_ORIGINS_ENV = "WEBHUB_FEED_TRUSTED_ORIGINS"

FetchStatus = Literal["ok", "not_modified", "error"]


@dataclass(frozen=True, slots=True)
class FeedFetchResult:
    status: FetchStatus
    body: bytes = b""
    etag: str | None = None
    last_modified: str | None = None
    error: str | None = None


FeedFetcher = Callable[[str, str | None, str | None], Awaitable[FeedFetchResult]]


def _header(value: str | None, limit: int) -> str | None:
    if not value:
        return None
    value = value.strip()
    return value if value and len(value) <= limit and value.isprintable() else None


def _origin(url: str) -> str | None:
    try:
        parsed = urlsplit(url.strip())
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").rstrip(".").lower()
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError:
        return None
    if scheme not in {"http", "https"} or not host:
        return None
    return f"{scheme}://{host}:{port}"


def trusted_origins() -> frozenset[str]:
    """Origins the operator explicitly allows to be private (e.g. a local RSSHub).

    ``WEBHUB_FEED_TRUSTED_ORIGINS=http://rsshub:1200,http://127.0.0.1:1200``.
    Everything else stays public-only.  Read per call so tests and operators
    need no restart-time wiring.
    """

    raw = os.environ.get(TRUSTED_ORIGINS_ENV, "")
    return frozenset(origin for part in raw.split(",") if (origin := _origin(part)) is not None)


def _stream(
    url: str, headers: Mapping[str, str]
) -> AbstractAsyncContextManager[tuple[str, httpx.Response]]:
    if _origin(url) in trusted_origins():
        # Same pinned, non-redirecting client; only the private-address check is
        # waived, and only for this exact origin (every redirect hop re-checks).
        return _pinned_stream(
            url, timeout_seconds=FETCH_TIMEOUT_SECONDS, allow_private=True, headers=headers
        )
    return pinned_public_stream(url, timeout_seconds=FETCH_TIMEOUT_SECONDS, headers=headers)


async def fetch_feed(
    url: str,
    etag: str | None,
    last_modified: str | None,
) -> FeedFetchResult:
    current = url.strip()
    headers = {"user-agent": _USER_AGENT, "accept": _ACCEPT, "accept-encoding": "gzip, deflate"}
    if etag:
        headers["if-none-match"] = etag
    if last_modified:
        headers["if-modified-since"] = last_modified
    try:
        for _hop in range(MAX_REDIRECTS + 1):
            if not current.lower().startswith(("http://", "https://")):
                return FeedFetchResult("error", error="unsafe_target")
            async with _stream(current, headers) as (request_url, response):
                status = response.status_code
                if status == 304:
                    return FeedFetchResult("not_modified", etag=etag, last_modified=last_modified)
                if 300 <= status < 400:
                    location = response.headers.get("location", "").strip()
                    if not location:
                        return FeedFetchResult("error", error="http_error")
                    current = urljoin(request_url, location)
                    continue
                if status == 429 or status >= 500:
                    return FeedFetchResult("error", error="http_unavailable")
                if not 200 <= status < 300:
                    return FeedFetchResult("error", error="http_error")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_FEED_BYTES:
                        return FeedFetchResult("error", error="too_large")
                return FeedFetchResult(
                    "ok",
                    body=bytes(body),
                    etag=_header(response.headers.get("etag"), 256),
                    last_modified=_header(response.headers.get("last-modified"), 128),
                )
        return FeedFetchResult("error", error="too_many_redirects")
    except ProviderTargetError:
        return FeedFetchResult("error", error="unsafe_target")
    except Exception:  # noqa: BLE001 - network failures of every kind are one outcome
        return FeedFetchResult("error", error="network_error")
