"""Fetch, validate and cache one remote image for the browser (Q56).

Favicons, cover previews and feed thumbnails are URLs taken from third-party
pages.  Letting the browser load them directly leaks every visit to those
hosts and lets a hostile page point WebHub's own UI at anything.  This module
is the only place such an image is fetched for display, and it reuses the
outbound path page analysis already trusts:

* **Every hop is validated and DNS-pinned.**  ``ingestion.fetcher`` resolves
  each hop, refuses private / loopback / reserved answers (recovering only a
  proxy Fake-IP through independent public DNS), and connects to the approved
  address with the original Host and SNI.  Redirects are followed manually.
* **Nothing from the browser is forwarded.**  The outbound request carries a
  fixed header set: no cookie, no referer, no authorization.  A new HTTP
  client per hop means a ``Set-Cookie`` on one hop never reaches the next,
  and upstream response headers are never copied back to the browser.
* **Only images, proven by their bytes.**  The declared type must be an image
  type (or a generic octet type) and the magic bytes must agree.  The response
  type served to the browser is the *detected* one, never the upstream header.
* **SVG is held to a stricter policy.**  As in favicon validation, an SVG is
  accepted only when the server explicitly declares ``image/svg+xml``; it must
  also be small, valid UTF-8 and free of scripts, event handlers, external
  references, entity declarations and other active content.  Every proxied
  response is additionally served with a sandboxing CSP and ``nosniff``.
* **Bounded everywhere.**  Size caps while streaming (decoded bytes),
  per-request and total deadlines, a global fetch concurrency limit, per-URL
  single flight, and a size-capped disk cache with failure cool-down.

Failures never carry remote text; they are reduced to a fixed code.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import urldefrag, urljoin, urlsplit

from webhub.ingestion.fetcher import (
    _FAKE_IP_RESOLUTION_CACHE,
    _GENERIC_ICON_CONTENT_TYPES,
    _ICON_CONTENT_TYPE_ALIASES,
    _ICON_CONTENT_TYPES,
    _MAX_ICON_SIGNATURE_BYTES,
    _detected_image_content_type,
    _pinned_stream,
    _valid_image_signature,
)
from webhub.providers.targets import ProviderTargetError

if TYPE_CHECKING:
    import httpx

MAX_URL_LENGTH = 4_096
MAX_REDIRECTS = 3
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_SVG_BYTES = 256 * 1024
REQUEST_TIMEOUT_SECONDS = 6
TOTAL_TIMEOUT_SECONDS = 12
SUCCESS_TTL_SECONDS = 7 * 24 * 60 * 60
FAILURE_COOLDOWN_SECONDS = 10 * 60
BROWSER_MAX_AGE_SECONDS = 24 * 60 * 60
STALE_BROWSER_MAX_AGE_SECONDS = 60
MAX_CONCURRENT_FETCHES = 6
CACHE_MAX_BYTES = 256 * 1024 * 1024
CACHE_PRUNE_TARGET_RATIO = 0.8
CACHE_PRUNE_INTERVAL_SECONDS = 5 * 60
CACHE_DIRECTORY_NAME = "media-cache"
_CACHE_FORMAT_VERSION = 1
_USER_AGENT = "WebHub/0.1 (+https://github.com/webhub)"
_OUTBOUND_HEADERS = {
    "user-agent": _USER_AGENT,
    "accept": "image/avif,image/webp,image/png,image/svg+xml,image/*;q=0.8",
    "accept-encoding": "gzip, deflate",
}
_SVG = "image/svg+xml"
_LOGGER = logging.getLogger(__name__)

FailureCode = Literal[
    "invalid_url",
    "unsafe_target",
    "upstream_error",
    "not_image",
    "unsafe_svg",
    "too_large",
]
_FAILURE_CODES: frozenset[str] = frozenset(
    {"invalid_url", "unsafe_target", "upstream_error", "not_image", "unsafe_svg", "too_large"}
)


class MediaProxyError(Exception):
    """One fixed failure code; never the remote server's own text."""

    def __init__(self, code: FailureCode, *, retry_after: int | None = None) -> None:
        super().__init__(code)
        self.code: FailureCode = code
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class FetchedImage:
    body: bytes
    content_type: str


@dataclass(frozen=True, slots=True)
class ProxiedImage:
    body: bytes
    content_type: str
    etag: str
    expires_at: float
    stale: bool = False


ImageFetcher = Callable[[str], Awaitable[FetchedImage]]
Clock = Callable[[], float]


# --------------------------------------------------------------------------- URL


def normalize_image_url(value: str) -> str:
    """Return the absolute http(s) URL that is fetched and used as cache key.

    The fragment is client-side only and is dropped; credentials are refused
    because the proxy must never send any form of authentication upstream.
    """

    if not isinstance(value, str):
        raise MediaProxyError("invalid_url")
    candidate = value.strip()
    if not candidate or len(candidate) > MAX_URL_LENGTH:
        raise MediaProxyError("invalid_url")
    if any(ord(character) < 0x21 or ord(character) == 0x7F for character in candidate):
        raise MediaProxyError("invalid_url")
    try:
        candidate = urldefrag(candidate).url
        parts = urlsplit(candidate)
        _ = parts.port
    except ValueError as error:
        raise MediaProxyError("invalid_url") from error
    if (
        parts.scheme.casefold() not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
    ):
        raise MediaProxyError("invalid_url")
    return candidate


def cache_key(normalized_url: str) -> str:
    return hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()


def content_etag(body: bytes) -> str:
    return '"' + hashlib.sha256(body).hexdigest()[:40] + '"'


# --------------------------------------------------------------------------- SVG

# Matched against the case-folded document.  Strict on purpose: a false
# rejection only means the UI falls back to its own icon, while a false
# acceptance would put active content on WebHub's origin.
_SVG_FORBIDDEN = tuple(
    re.compile(pattern)
    for pattern in (
        r"<!entity",
        r"<!doctype[^>]*\[",
        r"<\?xml-stylesheet",
        r"<(?:[\w.-]+:)?(?:script|foreignobject|iframe|embed|object|audio|video|canvas"
        r"|handler|listener|a)[\s/>]",
        # Event handlers, with or without a namespace prefix.
        r"[\s/\"'](?:[\w.-]+:)?on[a-z]+\s*=",
        # Only same-document fragment references are allowed.
        r"href\s*=\s*(?![\"']\s*#)",
        r"[\s/\"']src\s*=",
        r"url\(\s*(?![\"']?\s*#)",
        r"@import",
        r"javascript:",
        r"vbscript:",
        r"data:",
        # <set>/<animate> can rewrite href or install a handler at runtime.
        r"attributename\s*=\s*[\"']?\s*(?:[\w.-]+:)?(?:href|on)",
        # Character references could hide any of the above from this scan.
        r"&#",
    )
)


def svg_is_safe(body: bytes) -> bool:
    if not body or len(body) > MAX_SVG_BYTES:
        return False
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False
    if "\x00" in text:
        return False
    lowered = text.casefold()
    if _detected_image_content_type(body[:_MAX_ICON_SIGNATURE_BYTES]) != _SVG:
        return False
    return not any(pattern.search(lowered) for pattern in _SVG_FORBIDDEN)


def validated_image(declared_type: str, body: bytes) -> FetchedImage:
    """Prove ``body`` is an allowed image; return it with the detected type."""

    content_type = declared_type.split(";", 1)[0].strip().lower()
    normalized = _ICON_CONTENT_TYPE_ALIASES.get(content_type, content_type)
    if normalized not in _ICON_CONTENT_TYPES and normalized not in _GENERIC_ICON_CONTENT_TYPES:
        raise MediaProxyError("not_image")
    if not body:
        raise MediaProxyError("not_image")
    prefix = body[:_MAX_ICON_SIGNATURE_BYTES]
    detected = _detected_image_content_type(prefix)
    if detected is None or not _valid_image_signature(content_type, prefix):
        # A generic type carrying SVG lands here too: the favicon policy only
        # trusts SVG that the server explicitly declared as SVG.
        raise MediaProxyError("unsafe_svg" if detected == _SVG else "not_image")
    if detected == _SVG and not svg_is_safe(body):
        raise MediaProxyError("unsafe_svg")
    if detected != _SVG and len(body) > MAX_IMAGE_BYTES:
        raise MediaProxyError("too_large")
    return FetchedImage(body=body, content_type=detected)


# ------------------------------------------------------------------------- fetch


async def _read_capped(response: httpx.Response, limit: int) -> bytes:
    content_length = response.headers.get("content-length", "").strip()
    if content_length.isdigit() and int(content_length) > limit:
        raise MediaProxyError("too_large")
    body = bytearray()
    # aiter_bytes yields *decoded* bytes, so the cap also bounds a gzip bomb.
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > limit:
            raise MediaProxyError("too_large")
    return bytes(body)


async def _fetch(url: str) -> FetchedImage:
    import httpx

    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        try:
            async with _pinned_stream(
                current,
                timeout_seconds=REQUEST_TIMEOUT_SECONDS,
                allow_private=False,
                headers=_OUTBOUND_HEADERS,
            ) as (request_url, response):
                if 300 <= response.status_code < 400:
                    location = response.headers.get("location", "").strip()
                    if not location:
                        raise MediaProxyError("upstream_error")
                    # Resolve against the logical URL, not the pinned wire URL.
                    current = urldefrag(urljoin(request_url, location)).url
                    if urlsplit(current).scheme.casefold() not in {"http", "https"}:
                        raise MediaProxyError("unsafe_target")
                    continue
                if not 200 <= response.status_code < 300:
                    raise MediaProxyError("upstream_error")
                declared = response.headers.get("content-type", "")
                content_type = declared.split(";", 1)[0].strip().lower()
                normalized = _ICON_CONTENT_TYPE_ALIASES.get(content_type, content_type)
                if (
                    normalized not in _ICON_CONTENT_TYPES
                    and normalized not in _GENERIC_ICON_CONTENT_TYPES
                ):
                    raise MediaProxyError("not_image")
                limit = MAX_SVG_BYTES if normalized == _SVG else MAX_IMAGE_BYTES
                body = await _read_capped(response, limit)
        except ProviderTargetError as error:
            code: FailureCode = (
                "upstream_error"
                if error.code == "resource_public_dns_unreachable"
                else "unsafe_target"
            )
            raise MediaProxyError(code) from error
        except httpx.HTTPError as error:
            raise MediaProxyError("upstream_error") from error
        return validated_image(declared, body)
    raise MediaProxyError("upstream_error")


async def fetch_image(url: str) -> FetchedImage:
    """One public-only, pinned, redirect-revalidated image fetch; never leaks text."""

    token = _FAKE_IP_RESOLUTION_CACHE.set({})
    try:
        async with asyncio.timeout(TOTAL_TIMEOUT_SECONDS):
            return await _fetch(normalize_image_url(url))
    except TimeoutError as error:
        raise MediaProxyError("upstream_error") from error
    finally:
        _FAKE_IP_RESOLUTION_CACHE.reset(token)


# ------------------------------------------------------------------------- cache


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    body: bytes
    content_type: str
    etag: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class _FailureEntry:
    code: FailureCode
    until: float


class MediaCache:
    """Files under ``<data>/media-cache/<k[:2]>/<k>.img`` (and ``.fail``).

    Each image file is one JSON header line followed by the raw body and is
    written through a temporary file plus ``os.replace``, so a reader never
    sees a torn entry.  Entries are re-validated when read: a damaged or
    tampered file is discarded rather than served.
    """

    def __init__(self, directory: Path, *, max_bytes: int = CACHE_MAX_BYTES) -> None:
        self.directory = directory
        self.max_bytes = max_bytes

    def _path(self, key: str, suffix: str) -> Path:
        return self.directory / key[:2] / f"{key}{suffix}"

    def _write_atomic(self, path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
        try:
            with open(temporary, "wb") as handle:
                handle.write(payload)
            os.replace(temporary, path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()

    def read(self, key: str, url: str) -> _CacheEntry | None:
        path = self._path(key, ".img")
        try:
            raw = path.read_bytes()
        except OSError:
            return None
        header, separator, body = raw.partition(b"\n")
        try:
            meta = json.loads(header) if separator else None
            if not isinstance(meta, dict) or meta.get("v") != _CACHE_FORMAT_VERSION:
                raise ValueError("bad header")
            if meta.get("url") != url:
                return None
            content_type = str(meta["content_type"])
            expires_at = float(meta["expires_at"])
            validated = validated_image(content_type, body)
            if validated.content_type != content_type:
                raise ValueError("type mismatch")
        except (KeyError, TypeError, ValueError, MediaProxyError):
            with contextlib.suppress(OSError):
                path.unlink()
            return None
        with contextlib.suppress(OSError):
            os.utime(path)  # approximate LRU for pruning
        return _CacheEntry(body, content_type, content_etag(body), expires_at)

    def write(self, key: str, url: str, image: FetchedImage, expires_at: float) -> None:
        header = json.dumps(
            {
                "v": _CACHE_FORMAT_VERSION,
                "url": url,
                "content_type": image.content_type,
                "expires_at": expires_at,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        self._write_atomic(self._path(key, ".img"), header + b"\n" + image.body)
        with contextlib.suppress(OSError):
            self._path(key, ".fail").unlink()

    def read_failure(self, key: str) -> _FailureEntry | None:
        path = self._path(key, ".fail")
        try:
            meta = json.loads(path.read_bytes())
            code = meta["code"]
            until = float(meta["until"])
            if code not in _FAILURE_CODES:
                raise ValueError("unknown code")
        except FileNotFoundError:
            return None
        except (OSError, KeyError, TypeError, ValueError):
            with contextlib.suppress(OSError):
                path.unlink()
            return None
        return _FailureEntry(code=code, until=until)

    def write_failure(self, key: str, code: FailureCode, until: float) -> None:
        payload = json.dumps({"code": code, "until": until}).encode("utf-8")
        self._write_atomic(self._path(key, ".fail"), payload)

    def prune(self, now: float) -> None:
        """Drop expired cool-downs, then oldest images until under the cap."""

        if not self.directory.is_dir():
            return
        images: list[tuple[float, int, Path]] = []
        total = 0
        for path in self.directory.glob("*/*"):
            try:
                stat = path.stat()
            except OSError:
                continue
            if path.name.endswith(".tmp"):
                # Orphans of a crashed writer; live temp files are milliseconds old.
                if stat.st_mtime < now - 3600:
                    with contextlib.suppress(OSError):
                        path.unlink()
                continue
            if path.suffix == ".fail":
                failure = self.read_failure(path.stem)
                if failure is None or failure.until <= now:
                    with contextlib.suppress(OSError):
                        path.unlink()
                continue
            if path.suffix == ".img":
                images.append((stat.st_mtime, stat.st_size, path))
                total += stat.st_size
        if total <= self.max_bytes:
            return
        target = int(self.max_bytes * CACHE_PRUNE_TARGET_RATIO)
        for _mtime, size, path in sorted(images):
            if total <= target:
                break
            with contextlib.suppress(OSError):
                path.unlink()
                total -= size


# ------------------------------------------------------------------------- proxy


@dataclass(slots=True)
class _KeyGate:
    lock: asyncio.Lock
    references: int = 0


class MediaProxy:
    """Cache-first image proxy shared by the whole API process."""

    def __init__(
        self,
        directory: Path,
        *,
        fetcher: ImageFetcher | None = None,
        clock: Clock = time.time,
        success_ttl_seconds: int = SUCCESS_TTL_SECONDS,
        failure_cooldown_seconds: int = FAILURE_COOLDOWN_SECONDS,
        max_cache_bytes: int = CACHE_MAX_BYTES,
    ) -> None:
        self.cache = MediaCache(directory, max_bytes=max_cache_bytes)
        self._fetcher = fetcher
        self._clock = clock
        self._success_ttl = success_ttl_seconds
        self._failure_cooldown = failure_cooldown_seconds
        self._loop: asyncio.AbstractEventLoop | None = None
        self._gates: dict[str, _KeyGate] = {}
        self._semaphore: asyncio.Semaphore | None = None
        self._last_prune: float | None = None
        self._written_since_prune = 0
        self._pruning = False

    def now(self) -> float:
        return self._clock()

    def _loop_state(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        if self._loop is not loop or self._semaphore is None:
            self._loop = loop
            self._gates.clear()
            self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_FETCHES)
        return self._semaphore

    @asynccontextmanager
    async def _gate(self, key: str) -> AsyncIterator[None]:
        self._loop_state()
        gate = self._gates.get(key)
        if gate is None:
            gate = _KeyGate(lock=asyncio.Lock())
            self._gates[key] = gate
        gate.references += 1
        try:
            async with gate.lock:
                yield
        finally:
            gate.references -= 1
            if gate.references == 0 and self._gates.get(key) is gate:
                self._gates.pop(key, None)

    async def _fetch(self, url: str) -> FetchedImage:
        fetcher = self._fetcher or fetch_image
        return await fetcher(url)

    async def _cached(
        self, key: str, url: str
    ) -> tuple[_CacheEntry | None, _FailureEntry | None]:
        entry = await asyncio.to_thread(self.cache.read, key, url)
        failure = await asyncio.to_thread(self.cache.read_failure, key)
        return entry, failure

    def _served(self, entry: _CacheEntry, *, stale: bool) -> ProxiedImage:
        return ProxiedImage(
            body=entry.body,
            content_type=entry.content_type,
            etag=entry.etag,
            expires_at=entry.expires_at,
            stale=stale,
        )

    def _fresh_or_cooling(
        self, entry: _CacheEntry | None, failure: _FailureEntry | None, now: float
    ) -> ProxiedImage | None:
        if entry is not None and entry.expires_at > now:
            return self._served(entry, stale=False)
        if failure is not None and failure.until > now:
            if entry is not None:
                return self._served(entry, stale=True)
            raise MediaProxyError(failure.code, retry_after=max(1, int(failure.until - now)))
        return None

    async def get(self, url: str) -> ProxiedImage:
        normalized = normalize_image_url(url)
        key = cache_key(normalized)
        entry, failure = await self._cached(key, normalized)
        served = self._fresh_or_cooling(entry, failure, self._clock())
        if served is not None:
            return served

        async with self._gate(key):
            # A concurrent request for the same URL may have just filled it.
            entry, failure = await self._cached(key, normalized)
            served = self._fresh_or_cooling(entry, failure, self._clock())
            if served is not None:
                return served
            semaphore = self._loop_state()
            try:
                async with semaphore:
                    image = await self._fetch(normalized)
            except MediaProxyError as error:
                await self._record_failure(key, error.code)
                if entry is not None:
                    return self._served(entry, stale=True)
                raise MediaProxyError(
                    error.code, retry_after=self._failure_cooldown
                ) from None
            except Exception as error:  # noqa: BLE001 - contain bugs as an upstream failure
                _LOGGER.error("media proxy fetch failed (%s)", type(error).__name__)
                await self._record_failure(key, "upstream_error")
                if entry is not None:
                    return self._served(entry, stale=True)
                raise MediaProxyError(
                    "upstream_error", retry_after=self._failure_cooldown
                ) from None
            # A custom fetcher's result is re-proven like a cache read would be.
            image = validated_image(image.content_type, image.body)
            expires_at = self._clock() + self._success_ttl
            try:
                await asyncio.to_thread(self.cache.write, key, normalized, image, expires_at)
                self._written_since_prune += len(image.body)
                await self._maybe_prune()
            except OSError as error:
                _LOGGER.warning("media cache write failed (%s)", type(error).__name__)
            return ProxiedImage(
                body=image.body,
                content_type=image.content_type,
                etag=content_etag(image.body),
                expires_at=expires_at,
            )

    async def _record_failure(self, key: str, code: FailureCode) -> None:
        try:
            await asyncio.to_thread(
                self.cache.write_failure, key, code, self._clock() + self._failure_cooldown
            )
        except OSError as error:
            _LOGGER.warning("media cache failure write failed (%s)", type(error).__name__)

    async def _maybe_prune(self) -> None:
        """Prune on a timer, and early once enough was written to break the cap.

        Without the byte trigger, a burst of large previews between two timed
        prunes could write far more than ``max_bytes`` to the data disk.
        """

        now = self._clock()
        headroom = self.cache.max_bytes - int(self.cache.max_bytes * CACHE_PRUNE_TARGET_RATIO)
        due = (
            self._last_prune is None
            or now - self._last_prune >= CACHE_PRUNE_INTERVAL_SECONDS
            or self._written_since_prune >= headroom
        )
        if not due or self._pruning:
            return
        self._pruning = True
        self._last_prune = now
        self._written_since_prune = 0
        try:
            await asyncio.to_thread(self.cache.prune, now)
        finally:
            self._pruning = False


__all__ = [
    "BROWSER_MAX_AGE_SECONDS",
    "CACHE_DIRECTORY_NAME",
    "FAILURE_COOLDOWN_SECONDS",
    "MAX_IMAGE_BYTES",
    "MAX_SVG_BYTES",
    "MAX_URL_LENGTH",
    "STALE_BROWSER_MAX_AGE_SECONDS",
    "SUCCESS_TTL_SECONDS",
    "FetchedImage",
    "MediaCache",
    "MediaProxy",
    "MediaProxyError",
    "ProxiedImage",
    "cache_key",
    "content_etag",
    "fetch_image",
    "normalize_image_url",
    "svg_is_safe",
    "validated_image",
]
