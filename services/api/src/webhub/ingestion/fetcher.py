"""Fetch one user-supplied URL safely enough to read its metadata.

Every other outbound call in WebHub targets an address we control (a vendor's
API).  This one targets whatever the *user* pasted, which makes it the natural
home for a server-side request forgery.  The defences, in the order they run:

* **Every hop is validated, not just the first.**  Redirects are followed
  manually — ``follow_redirects=False`` — and each new location goes through
  a fresh resource-target resolution.  Following redirects automatically
  would mean one ``302`` to ``169.254.169.254`` walks straight past the check.
* **DNS is resolved and pinned at each hop.**  The HTTP connection is made to
  the approved IP while the original Host and TLS SNI are retained.  This
  closes the validation/request DNS rebinding window.
* **Only ``text/html`` is read.**  A wrong content type is reported as
  ``limited``, not parsed and not guessed at.
* **The body is capped and streamed**, so a hostile endpoint cannot stream
  gigabytes into memory.
* **Nothing is executed.**  No JS engine, no headless browser — a page that
  only renders client-side simply yields no metadata, which is reported
  honestly as ``limited``.

Failure never raises to the caller: a site that cannot be analysed must still
be storable.  Outcomes map onto the ``analysis_status`` column.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal
from urllib.parse import urljoin, urlsplit

from webhub.providers.targets import ProviderTargetError, resolve_resource_target

from .metadata import parse_metadata

if TYPE_CHECKING:
    import httpx

MAX_REDIRECTS = 3
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_ICON_BYTES = 256 * 1024
MAX_ICON_CANDIDATE_TIMEOUT_SECONDS = 2
MAX_ROOT_ICON_RESERVED_SECONDS = 4
DEFAULT_TIMEOUT_SECONDS = 8
_USER_AGENT = "WebHub/0.1 (+https://github.com/webhub)"
_ROOT_ICON_PATHS = (
    "/favicon.ico",
    "/favicon.png",
    "/favicon.svg",
    "/apple-touch-icon.png",
)
_ICON_CONTENT_TYPES = frozenset(
    {
        "image/avif",
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/svg+xml",
        "image/vnd.microsoft.icon",
        "image/webp",
        "image/x-icon",
    }
)
_GENERIC_ICON_CONTENT_TYPES = frozenset(
    {
        "",
        "application/octet-stream",
        "binary/octet-stream",
        "image",
        "image/*",
    }
)
_ICON_CONTENT_TYPE_ALIASES = {
    "application/ico": "image/x-icon",
    "image/ico": "image/x-icon",
    "image/jpg": "image/jpeg",
    "image/svg": "image/svg+xml",
    "image/vnd.microsoft.icon": "image/x-icon",
    "image/x-png": "image/png",
}
_MAX_ICON_SIGNATURE_BYTES = 4 * 1024
_LOGGER = logging.getLogger(__name__)

AnalysisStatus = Literal["complete", "failed", "limited"]


@dataclass(frozen=True, slots=True)
class SiteMetadata:
    title: str | None = None
    description: str | None = None
    image_url: str | None = None
    icon_url: str | None = None
    related_urls: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FetchOutcome:
    """One analysis attempt, already reduced to what the column stores."""

    status: AnalysisStatus
    reason: str
    metadata: SiteMetadata = SiteMetadata()
    final_url: str | None = None

    @property
    def is_complete(self) -> bool:
        return self.status == "complete"


# Every failure the user can see, in Chinese, composed here rather than taken
# from the remote server.  A fetched page's own error text is untrusted content.
_REASONS: dict[str, str] = {
    "unsafe_target": "该地址指向本机、内网或其他不允许访问的目标，已拒绝访问",
    "unreachable": "无法连接到该网站",
    "timeout": "访问该网站超时",
    "too_many_redirects": "跳转次数过多，已停止跟随",
    "http_error": "该网站返回了错误状态，无法读取内容",
    "not_html": "该地址返回的不是网页内容，已跳过分析",
    "too_large": "网页内容过大，已停止读取",
    "no_metadata": "该网页没有可提取的标题或描述（可能需要执行脚本才能渲染）",
    "icon_only": "网页内容无法读取，但已获取网站图标",
    "ok": "已提取网页元数据",
}


def _reason(code: str) -> str:
    return _REASONS.get(code, "分析失败")


def _outcome(status: AnalysisStatus, code: str) -> FetchOutcome:
    return FetchOutcome(status=status, reason=_reason(code))


def _origin(url: str) -> tuple[str, str, int | None] | None:
    try:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            return None
        scheme = parts.scheme.lower()
        return scheme, parts.hostname.casefold(), parts.port or (443 if scheme == "https" else 80)
    except ValueError:
        return None


def _log_origin(url: str) -> str:
    origin = _origin(url)
    if origin is None:
        return "<invalid-url>"
    scheme, hostname, port = origin
    default_port = (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    host = f"[{hostname}]" if ":" in hostname else hostname
    return f"{scheme}://{host}" if default_port else f"{scheme}://{host}:{port}"


@asynccontextmanager
async def _pinned_stream(
    url: str,
    *,
    timeout_seconds: int,
    allow_private: bool,
    headers: Mapping[str, str],
) -> AsyncIterator[tuple[str, httpx.Response]]:
    """Resolve once, then connect to that exact IP with the logical host intact."""

    import httpx

    target = await resolve_resource_target(
        url,
        allow_private=allow_private,
        timeout_seconds=timeout_seconds,
    )
    request_headers = dict(headers)
    request_headers["host"] = target.host_header
    limits = httpx.Limits(max_connections=1, max_keepalive_connections=0)
    # One client per outbound request prevents a connection validated for one
    # hop from being reused for a later hop.  trust_env=False also prevents an
    # environment proxy from replacing the pinned destination.
    async with httpx.AsyncClient(
        timeout=timeout_seconds,
        follow_redirects=False,
        trust_env=False,
        limits=limits,
    ) as client, client.stream(
        "GET",
        target.connection_url(),
        headers=request_headers,
        extensions={"sni_hostname": target.hostname},
    ) as response:
        yield target.url, response


def _detected_image_content_type(prefix: bytes) -> str | None:
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if prefix.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if prefix.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(prefix) >= 12 and prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP":
        return "image/webp"
    if prefix.startswith((b"\x00\x00\x01\x00", b"\x00\x00\x02\x00")):
        return "image/x-icon"
    if len(prefix) >= 12 and prefix[4:8] == b"ftyp":
        brands = {prefix[8:12]}
        brands.update(
            prefix[index : index + 4] for index in range(16, len(prefix) - 3, 4)
        )
        if brands.intersection({b"avif", b"avis"}):
            return "image/avif"
    candidate = prefix.removeprefix(b"\xef\xbb\xbf").lstrip()
    if re.match(
        rb"^(?:<\?xml[^>]*>\s*)?(?:<!--.*?-->\s*)?"
        rb"(?:<!doctype\s+svg[^>]*>\s*)?<svg(?:\s|>)",
        candidate,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        return "image/svg+xml"
    return None


def _valid_image_signature(content_type: str, prefix: bytes) -> bool:
    detected = _detected_image_content_type(prefix)
    if detected is None:
        return False
    normalized_type = _ICON_CONTENT_TYPE_ALIASES.get(content_type, content_type)
    if normalized_type in _GENERIC_ICON_CONTENT_TYPES:
        return detected != "image/svg+xml"
    if normalized_type not in _ICON_CONTENT_TYPES:
        return False
    return normalized_type == detected


async def _validated_public_resource_url(url: str | None, *, timeout_seconds: int) -> str | None:
    """Keep a metadata URL only when its current DNS answer is entirely public."""

    if not url:
        return None
    try:
        target = await resolve_resource_target(
            url,
            allow_private=False,
            timeout_seconds=timeout_seconds,
        )
    except ProviderTargetError:
        return None
    return target.url


async def _validated_icon_url(
    url: str,
    *,
    timeout_seconds: int,
    allow_private: bool,
) -> str | None:
    """Return an icon URL only after a bounded, SSRF-safe image fetch succeeds."""

    current = url.strip()
    for _hop in range(MAX_REDIRECTS + 1):
        if _origin(current) is None:
            return None
        try:
            async with _pinned_stream(
                current,
                timeout_seconds=timeout_seconds,
                allow_private=allow_private,
                headers={
                    "user-agent": _USER_AGENT,
                    "accept": "image/avif,image/webp,image/png,image/svg+xml,image/*;q=0.8",
                    "accept-encoding": "gzip, deflate",
                },
            ) as (request_url, response):
                if 300 <= response.status_code < 400:
                    location = response.headers.get("location", "").strip()
                    if not location:
                        return None
                    current = urljoin(request_url, location)
                    continue
                if not 200 <= response.status_code < 300:
                    return None
                content_type = (
                    response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                )
                normalized_type = _ICON_CONTENT_TYPE_ALIASES.get(content_type, content_type)
                if (
                    normalized_type not in _ICON_CONTENT_TYPES
                    and normalized_type not in _GENERIC_ICON_CONTENT_TYPES
                ):
                    return None
                content_length = response.headers.get("content-length", "").strip()
                if content_length.isdigit() and int(content_length) > MAX_ICON_BYTES:
                    return None
                size = 0
                signature = bytearray()
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_ICON_BYTES:
                        return None
                    remaining = _MAX_ICON_SIGNATURE_BYTES - len(signature)
                    if remaining > 0:
                        signature.extend(chunk[:remaining])
                if size == 0 or not _valid_image_signature(content_type, bytes(signature)):
                    return None
                return request_url
        except Exception as error:  # noqa: BLE001 - optional icon failure stays contained
            _LOGGER.warning(
                "favicon fetch failed for %s (%s)",
                _log_origin(current),
                type(error).__name__,
            )
            return None
    return None


async def _discover_icon_url(
    *,
    page_url: str,
    declared_urls: tuple[str, ...] = (),
    timeout_seconds: int,
    allow_private: bool,
) -> str | None:
    """Try declared icons, then a bounded same-origin root fallback list."""

    if _origin(page_url) is None:
        return None
    seen: set[str] = set()
    candidate_timeout = min(timeout_seconds, MAX_ICON_CANDIDATE_TIMEOUT_SECONDS)

    async def validate(candidate: str, wall_timeout: float) -> str | None:
        try:
            async with asyncio.timeout(wall_timeout):
                return await _validated_icon_url(
                    candidate,
                    timeout_seconds=wall_timeout,
                    allow_private=allow_private,
                )
        except TimeoutError:
            return None

    async def first_valid(candidates: tuple[str, ...], wall_timeout: float) -> str | None:
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            icon_url = await validate(candidate, wall_timeout)
            if icon_url is not None:
                return icon_url
        return None

    try:
        async with asyncio.timeout(timeout_seconds):
            root_budget = timeout_seconds
            if declared_urls:
                root_reserve = min(MAX_ROOT_ICON_RESERVED_SECONDS, timeout_seconds / 2)
                declared_budget = timeout_seconds - root_reserve
                root_budget = root_reserve
                try:
                    async with asyncio.timeout(declared_budget):
                        declared_icon = await first_valid(declared_urls, candidate_timeout)
                except TimeoutError:
                    declared_icon = None
                if declared_icon is not None:
                    return declared_icon

            root_candidates = tuple(urljoin(page_url, path) for path in _ROOT_ICON_PATHS)
            root_candidate_timeout = min(
                candidate_timeout,
                root_budget / len(root_candidates),
            )
            return await first_valid(root_candidates, root_candidate_timeout)
    except TimeoutError:
        return None


async def _attach_root_icon(
    outcome: FetchOutcome,
    *,
    page_url: str,
    timeout_seconds: int,
    allow_private: bool,
) -> FetchOutcome:
    icon_url = await _discover_icon_url(
        page_url=page_url,
        timeout_seconds=timeout_seconds,
        allow_private=allow_private,
    )
    if icon_url is None:
        return outcome
    status = "limited" if outcome.status == "failed" else outcome.status
    reason = _reason("icon_only") if outcome.status == "failed" else outcome.reason
    return replace(
        outcome,
        status=status,
        reason=reason,
        metadata=replace(outcome.metadata, icon_url=icon_url),
        final_url=outcome.final_url or page_url,
    )


async def _decoded(body: bytes, content_type: str) -> str:
    charset = ""
    for part in content_type.split(";"):
        key, _, value = part.strip().partition("=")
        if key.strip().lower() == "charset":
            charset = value.strip().strip('"').lower()
            break
    for encoding in (charset, "utf-8", "gb18030", "latin-1"):
        if not encoding:
            continue
        try:
            return body.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", errors="replace")


async def fetch_site_metadata(
    url: str,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    allow_private: bool = False,
) -> FetchOutcome:
    """Fetch ``url`` and read its metadata, never raising.

    ``allow_private`` exists for tests and for a future "analyse my intranet
    wiki" opt-in; it defaults to off, and the default is what every caller in
    the product uses today.
    """

    import httpx

    current = url.strip()
    if not current.lower().startswith(("http://", "https://")):
        return _outcome("failed", "unsafe_target")

    try:
        for _hop in range(MAX_REDIRECTS + 1):
            try:
                async with _pinned_stream(
                    current,
                    timeout_seconds=timeout_seconds,
                    allow_private=allow_private,
                    headers={
                        "user-agent": _USER_AGENT,
                        "accept": "text/html,application/xhtml+xml",
                        # httpx handles gzip/deflate without extra dependencies.
                        "accept-encoding": "gzip, deflate",
                    },
                ) as (request_url, response):
                    if 300 <= response.status_code < 400:
                        location = response.headers.get("location", "").strip()
                        if not location:
                            return await _attach_root_icon(
                                _outcome("failed", "http_error"),
                                page_url=request_url,
                                timeout_seconds=timeout_seconds,
                                allow_private=allow_private,
                            )
                        # The wire URL contains the pinned IP.  Resolve relative
                        # redirects against the logical URL so Host/SNI cannot
                        # silently become that IP on the next hop.
                        current = urljoin(request_url, location)
                        if not current.lower().startswith(("http://", "https://")):
                            return _outcome("failed", "unsafe_target")
                        continue
                    if response.status_code != 200:
                        return await _attach_root_icon(
                            _outcome("failed", "http_error"),
                            page_url=request_url,
                            timeout_seconds=timeout_seconds,
                            allow_private=allow_private,
                        )

                    content_type = response.headers.get("content-type", "")
                    if "text/html" not in content_type.lower() and (
                        "application/xhtml" not in content_type.lower()
                    ):
                        # Trust the declared type only; sniffing would mean
                        # parsing bytes the server said were not HTML.
                        return await _attach_root_icon(
                            _outcome("limited", "not_html"),
                            page_url=request_url,
                            timeout_seconds=timeout_seconds,
                            allow_private=allow_private,
                        )

                    body = bytearray()
                    truncated = False
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) >= MAX_BODY_BYTES:
                            truncated = True
                            break
            except ProviderTargetError:
                return _outcome("failed", "unsafe_target")

            final_url = request_url
            html = await _decoded(bytes(body), content_type)
            parsed = parse_metadata(html, base_url=final_url)
            image_url = await _validated_public_resource_url(
                parsed.image_url,
                timeout_seconds=timeout_seconds,
            )
            icon_url = await _discover_icon_url(
                page_url=final_url,
                declared_urls=tuple(parsed.icon_hrefs),
                timeout_seconds=timeout_seconds,
                allow_private=allow_private,
            )
            metadata = SiteMetadata(
                title=parsed.best_title,
                description=parsed.description,
                image_url=image_url,
                icon_url=icon_url,
                related_urls=tuple(parsed.github_links),
            )
            if metadata.title is None and metadata.description is None:
                # Truncation is the more useful explanation when both apply.
                code = "too_large" if truncated else "no_metadata"
                return FetchOutcome(
                    status="limited",
                    reason=_reason(code),
                    metadata=metadata,
                    final_url=final_url,
                )
            return FetchOutcome(
                status="complete",
                reason=_reason("ok"),
                metadata=metadata,
                final_url=final_url,
            )

        return _outcome("failed", "too_many_redirects")
    except httpx.TimeoutException:
        return await _attach_root_icon(
            _outcome("failed", "timeout"),
            page_url=current,
            timeout_seconds=timeout_seconds,
            allow_private=allow_private,
        )
    except httpx.TransportError:
        return await _attach_root_icon(
            _outcome("failed", "unreachable"),
            page_url=current,
            timeout_seconds=timeout_seconds,
            allow_private=allow_private,
        )
    except Exception as error:  # noqa: BLE001 - return a stable error, but never hide the bug
        _LOGGER.error(
            "unexpected site metadata fetch failure for %s (%s)",
            _log_origin(current),
            type(error).__name__,
        )
        return await _attach_root_icon(
            _outcome("failed", "unreachable"),
            page_url=current,
            timeout_seconds=timeout_seconds,
            allow_private=allow_private,
        )


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_BODY_BYTES",
    "MAX_ICON_CANDIDATE_TIMEOUT_SECONDS",
    "MAX_ICON_BYTES",
    "MAX_REDIRECTS",
    "MAX_ROOT_ICON_RESERVED_SECONDS",
    "AnalysisStatus",
    "FetchOutcome",
    "SiteMetadata",
    "fetch_site_metadata",
]
