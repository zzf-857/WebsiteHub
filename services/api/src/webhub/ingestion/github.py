"""GitHub repository enrichment (Q45).

When analysis meets a ``kind=repo`` resource hosted on github.com, two GitHub
REST calls add what a repository page's HTML does not reliably expose:

* ``GET /repos/{owner}/{repo}`` — stars, forks, primary language, topics,
  license, ``pushed_at``, ``archived``, description and homepage.  The
  numbers are stored under ``sites.meta_json["github"]`` for covers and the
  detail page; they are facts about the repository, not user decisions.
* ``GET /repos/{owner}/{repo}/readme`` (raw) — only when an LLM analysis will
  run.  At most the first 4 KB are read and handed to the model as evidence;
  the README text is never stored.

Rules that shape the code:

* **Same outbound path as page analysis.**  Every request goes through
  ``fetcher.pinned_public_stream``: SSRF-checked, DNS-pinned, no environment
  proxy, no automatic redirects.  A renamed repository answers ``301`` to
  another ``api.github.com`` URL; exactly one such hop is followed.
* **Quota is precious.**  Anonymous access is 60 requests per hour per IP.
  Stats are reused for 7 days unless a person asked for a re-analysis, the
  repository call is conditional on the stored ETag (a ``304`` costs nothing
  on an authenticated request), and ``403`` / ``429`` start a process-wide
  backoff per credential.  While backing off no request is sent at all, and
  the analysis itself carries on without GitHub data.
* **No GitHub text reaches the client except parsed, bounded fields.**  Error
  bodies are never read.
* **The optional account token** comes from an enabled ``code_host`` Provider
  (GitHub, fixed HTTPS origin).  A rejected token falls back to one anonymous
  attempt so an expired token never hides public data.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, Protocol
from urllib.parse import quote, urlsplit

from .fetcher import DEFAULT_TIMEOUT_SECONDS, pinned_public_stream

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_LOGGER = logging.getLogger(__name__)

GITHUB_HOST = "github.com"
GITHUB_API_ORIGIN = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"
STATS_TTL = timedelta(days=7)
README_EXCERPT_BYTES = 4 * 1024
MAX_REPOSITORY_JSON_BYTES = 512 * 1024
DEFAULT_BACKOFF_SECONDS = 60
MAX_BACKOFF_SECONDS = 60 * 60
MAX_TOPICS = 20
MAX_LANGUAGE_CHARS = 60
MAX_LICENSE_CHARS = 80
MAX_DESCRIPTION_CHARS = 350
MAX_HOMEPAGE_CHARS = 2_048
MAX_ETAG_CHARS = 256
ANONYMOUS_CREDENTIAL_KEY = "anonymous"

_USER_AGENT = "WebHub/0.1 (+https://github.com/webhub)"
_OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_REPO = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_TOPIC = re.compile(r"^[a-z0-9][a-z0-9-]{0,49}$")
# Header values are echoed back in If-None-Match: only a well-formed entity
# tag may ever be sent, so a damaged stored value cannot inject a header.
_ETAG = re.compile(r'^(?:W/)?"[\x21\x23-\x7e]{1,250}"$')
_REDIRECT_STATUSES = frozenset({301, 302, 307, 308})
_RATE_LIMIT_STATUSES = frozenset({403, 429})

GitHubStatus = Literal["ok", "not_modified", "not_found", "rate_limited", "failed", "skipped"]

# Keys this module owns inside ``meta_json["github"]``.
_STAT_KEYS = (
    "stars",
    "forks",
    "language",
    "topics",
    "license",
    "pushed_at",
    "archived",
    "description",
    "homepage",
)


@dataclass(frozen=True, slots=True)
class GitHubRepoRef:
    owner: str
    repo: str

    @property
    def api_path(self) -> str:
        return f"/repos/{quote(self.owner, safe='')}/{quote(self.repo, safe='')}"

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"


@dataclass(frozen=True, slots=True)
class GitHubCredential:
    """An account's decrypted GitHub token; never printed."""

    config_id: str
    token: str = field(repr=False)

    @property
    def key(self) -> str:
        return f"account:{self.config_id}"


@dataclass(frozen=True, slots=True)
class GitHubRepoSnapshot:
    stars: int
    forks: int
    language: str | None
    topics: tuple[str, ...]
    license: str | None
    pushed_at: str | None
    archived: bool
    description: str | None
    homepage: str | None


@dataclass(frozen=True, slots=True)
class GitHubFetchResult:
    """``status`` describes the repository (stats) request only."""

    status: GitHubStatus
    snapshot: GitHubRepoSnapshot | None = None
    etag: str | None = None
    readme_excerpt: str = ""


CredentialResolver = Callable[["AsyncSession", str], Awaitable[GitHubCredential | None]]


class RepositoryEnricher(Protocol):
    async def credential(self, session: AsyncSession, user_id: str) -> GitHubCredential | None:
        """Return the account's GitHub token, or ``None`` for anonymous access."""

    async def fetch(
        self,
        ref: GitHubRepoRef,
        *,
        credential: GitHubCredential | None,
        etag: str | None,
        include_stats: bool,
        include_readme: bool,
    ) -> GitHubFetchResult:
        """Fetch stats and/or the README excerpt; never raises for remote failures."""


# ── process-wide backoff ─────────────────────────────────────────────────

_BACKOFF_UNTIL: dict[str, float] = {}


def reset_rate_limit_state() -> None:
    """Forget every backoff window (tests and process restarts)."""

    _BACKOFF_UNTIL.clear()


def backoff_remaining(credential_key: str) -> float:
    until = _BACKOFF_UNTIL.get(credential_key)
    if until is None:
        return 0.0
    remaining = until - time.monotonic()
    if remaining <= 0:
        _BACKOFF_UNTIL.pop(credential_key, None)
        return 0.0
    return remaining


def _header_int(headers: Mapping[str, str], name: str) -> int | None:
    value = (headers.get(name) or "").strip()
    if not value.isdigit():
        return None
    return int(value)


def _bounded_backoff(seconds: float) -> float:
    return float(min(max(seconds, 1.0), MAX_BACKOFF_SECONDS))


def _note_rate_limit(credential_key: str, status: int, headers: Mapping[str, str]) -> None:
    """Start or extend the backoff window this response asks for."""

    retry_after = _header_int(headers, "retry-after")
    remaining = _header_int(headers, "x-ratelimit-remaining")
    reset = _header_int(headers, "x-ratelimit-reset")
    delay: float | None = None
    if retry_after is not None:
        delay = float(retry_after)
    elif remaining == 0 and reset is not None:
        delay = reset - time.time()
    elif status in _RATE_LIMIT_STATUSES:
        # Secondary limits may come without headers; GitHub asks for at least
        # a minute before retrying.
        delay = float(DEFAULT_BACKOFF_SECONDS)
    if delay is None:
        return
    until = time.monotonic() + _bounded_backoff(delay)
    _BACKOFF_UNTIL[credential_key] = max(_BACKOFF_UNTIL.get(credential_key, 0.0), until)


# ── parsing ──────────────────────────────────────────────────────────────


def _collapse(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    collapsed = " ".join(value.split())
    return collapsed[:limit] or None


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _iso_utc(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_homepage(value: object) -> str | None:
    homepage = _collapse(value, MAX_HOMEPAGE_CHARS)
    if homepage is None:
        return None
    try:
        parsed = urlsplit(homepage)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    return homepage


def _topics(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    topics: list[str] = []
    for item in value:
        if isinstance(item, str) and _TOPIC.fullmatch(item) and item not in topics:
            topics.append(item)
        if len(topics) >= MAX_TOPICS:
            break
    return tuple(topics)


def _license(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    spdx = _collapse(value.get("spdx_id"), MAX_LICENSE_CHARS)
    if spdx and spdx.upper() != "NOASSERTION":
        return spdx
    return _collapse(value.get("name"), MAX_LICENSE_CHARS)


def parse_repository(payload: object) -> GitHubRepoSnapshot | None:
    """Map a ``GET /repos/{owner}/{repo}`` body to bounded fields."""

    if not isinstance(payload, Mapping):
        return None
    stars = _count(payload.get("stargazers_count"))
    forks = _count(payload.get("forks_count"))
    if stars is None or forks is None:
        return None
    return GitHubRepoSnapshot(
        stars=stars,
        forks=forks,
        language=_collapse(payload.get("language"), MAX_LANGUAGE_CHARS),
        topics=_topics(payload.get("topics")),
        license=_license(payload.get("license")),
        pushed_at=_iso_utc(payload.get("pushed_at")),
        archived=payload.get("archived") is True,
        description=_collapse(payload.get("description"), MAX_DESCRIPTION_CHARS),
        homepage=_safe_homepage(payload.get("homepage")),
    )


_HTML_COMMENT = re.compile(r"<!--.*?(?:-->|$)", re.DOTALL)
_MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_HTML_TAG = re.compile(r"<[^<>]{0,500}>")
_MARKDOWN_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def readme_excerpt(raw: bytes) -> str:
    """Readable text from the first 4 KB of a README (badges and markup removed)."""

    text = raw[:README_EXCERPT_BYTES].decode("utf-8", errors="ignore")
    text = _HTML_COMMENT.sub(" ", text)
    text = _MARKDOWN_IMAGE.sub(" ", text)
    text = _HTML_TAG.sub(" ", text)
    text = _MARKDOWN_LINK.sub(r"\1", text)
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def valid_etag(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > MAX_ETAG_CHARS:
        return None
    candidate = value.strip()
    return candidate if _ETAG.fullmatch(candidate) else None


def github_repo_ref(kind: object, meta: Mapping[str, Any] | None) -> GitHubRepoRef | None:
    """The github.com repository a stored resource points at, if any."""

    if kind != "repo" or not isinstance(meta, Mapping):
        return None
    host = meta.get("host")
    owner = meta.get("owner")
    repo = meta.get("repo")
    if not isinstance(host, str) or host.casefold().removeprefix("www.") != GITHUB_HOST:
        return None
    if not isinstance(owner, str) or not _OWNER.fullmatch(owner):
        return None
    if not isinstance(repo, str) or repo in {".", ".."} or not _REPO.fullmatch(repo):
        return None
    return GitHubRepoRef(owner=owner, repo=repo)


# ── stored metadata ──────────────────────────────────────────────────────


def _utc_text(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def stored_stats(previous: object) -> dict[str, Any] | None:
    """Re-validate the stats part of a stored ``github`` record."""

    if not isinstance(previous, Mapping) or previous.get("not_found") is True:
        return None
    stars = _count(previous.get("stars"))
    forks = _count(previous.get("forks"))
    if stars is None or forks is None:
        return None
    topics = previous.get("topics")
    return {
        "stars": stars,
        "forks": forks,
        "language": _collapse(previous.get("language"), MAX_LANGUAGE_CHARS),
        "topics": list(_topics(topics if isinstance(topics, list) else [])),
        "license": _collapse(previous.get("license"), MAX_LICENSE_CHARS),
        "pushed_at": _iso_utc(previous.get("pushed_at")),
        "archived": previous.get("archived") is True,
        "description": _collapse(previous.get("description"), MAX_DESCRIPTION_CHARS),
        "homepage": _safe_homepage(previous.get("homepage")),
    }


def stored_etag(previous: object) -> str | None:
    if stored_stats(previous) is None:
        # Without the stats a 304 would leave nothing to keep.
        return None
    assert isinstance(previous, Mapping)
    return valid_etag(previous.get("etag"))


def stats_are_fresh(previous: object, now: datetime) -> bool:
    """Stats (or a confirmed 404) younger than seven days are reused."""

    if not isinstance(previous, Mapping):
        return False
    if stored_stats(previous) is None and previous.get("not_found") is not True:
        return False
    fetched_at = _iso_utc(previous.get("fetched_at"))
    if fetched_at is None:
        return False
    fetched = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    return timedelta(0) <= now - fetched < STATS_TTL


def github_meta_update(
    result: GitHubFetchResult,
    previous: object,
    now: datetime,
) -> dict[str, Any] | None:
    """The new ``meta_json["github"]`` value, or ``None`` to leave it alone."""

    fetched_at = _utc_text(now)
    if result.status == "ok" and result.snapshot is not None:
        snapshot = result.snapshot
        return {
            "stars": snapshot.stars,
            "forks": snapshot.forks,
            "language": snapshot.language,
            "topics": list(snapshot.topics),
            "license": snapshot.license,
            "pushed_at": snapshot.pushed_at,
            "archived": snapshot.archived,
            "description": snapshot.description,
            "homepage": snapshot.homepage,
            "fetched_at": fetched_at,
            "etag": valid_etag(result.etag),
        }
    stats = stored_stats(previous)
    if result.status == "not_modified":
        if stats is None:
            return None
        etag = valid_etag(result.etag) or stored_etag(previous)
        return {**stats, "fetched_at": fetched_at, "etag": etag}
    if result.status == "not_found":
        # Deleted, renamed away or private: old numbers would now mislead.
        return {"not_found": True, "fetched_at": fetched_at}
    if result.status == "rate_limited":
        # Mark the attempt as limited and keep whatever is already known.
        kept: dict[str, Any] = {}
        if isinstance(previous, Mapping):
            if stats is not None:
                kept.update(stats)
                kept["etag"] = stored_etag(previous)
            elif previous.get("not_found") is True:
                kept["not_found"] = True
            previous_fetched = _iso_utc(previous.get("fetched_at"))
            if previous_fetched is not None:
                kept["fetched_at"] = previous_fetched
        return {**kept, "rate_limited_at": fetched_at}
    return None


def repository_evidence(
    ref: GitHubRepoRef,
    stats: Mapping[str, Any] | None,
    readme: str,
) -> str:
    """Plain-text repository facts handed to the model as low-trust evidence."""

    lines = [f"GitHub 仓库：{ref.full_name}"]
    if stats:
        if stats.get("description"):
            lines.append(f"仓库简介：{stats['description']}")
        if stats.get("language"):
            lines.append(f"主要语言：{stats['language']}")
        if stats.get("topics"):
            lines.append(f"主题标签：{', '.join(stats['topics'])}")
        if stats.get("license"):
            lines.append(f"许可证：{stats['license']}")
        if isinstance(stats.get("stars"), int):
            lines.append(f"星标数：{stats['stars']}")
        if stats.get("archived") is True:
            lines.append("状态：已归档（只读）")
    if readme:
        lines.append(f"README 摘录：\n{readme}")
    if len(lines) == 1:
        return ""
    return "\n".join(lines)


# ── HTTP ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Response:
    status: int
    headers: Mapping[str, str]
    body: bytes = b""


class _ResponseTooLarge(Exception):
    pass


async def _get(
    url: str,
    *,
    credential: GitHubCredential | None,
    accept: str,
    etag: str | None,
    max_bytes: int,
    truncate: bool,
    timeout_seconds: int,
) -> _Response:
    headers = {
        "user-agent": _USER_AGENT,
        "accept": accept,
        "x-github-api-version": GITHUB_API_VERSION,
    }
    if credential is not None:
        headers["authorization"] = f"Bearer {credential.token}"
    if etag is not None:
        headers["if-none-match"] = etag
    async with pinned_public_stream(
        url,
        timeout_seconds=timeout_seconds,
        headers=headers,
    ) as (_, response):
        selected = {
            name: response.headers.get(name, "")
            for name in (
                "etag",
                "location",
                "retry-after",
                "x-ratelimit-remaining",
                "x-ratelimit-reset",
            )
        }
        if response.status_code != 200:
            # Error bodies are never read: they are GitHub's text, not ours.
            return _Response(response.status_code, selected)
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) >= max_bytes:
                if truncate:
                    del body[max_bytes:]
                    break
                raise _ResponseTooLarge
        return _Response(200, selected, bytes(body))


def _api_redirect(location: str) -> str | None:
    """Accept only a redirect that stays on the official API origin."""

    try:
        parsed = urlsplit(location.strip())
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.hostname != "api.github.com":
        return None
    if parsed.port not in {None, 443} or parsed.username or parsed.password:
        return None
    return location.strip()


class GitHubRepositoryEnricher:
    """GitHub client used by site analysis; one instance per application."""

    def __init__(
        self,
        *,
        credentials: CredentialResolver | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        api_origin: str = GITHUB_API_ORIGIN,
    ) -> None:
        self._credentials = credentials
        self._timeout_seconds = timeout_seconds
        self._api_origin = api_origin.rstrip("/")

    async def credential(self, session: AsyncSession, user_id: str) -> GitHubCredential | None:
        if self._credentials is None:
            return None
        try:
            return await self._credentials(session, user_id)
        except Exception as error:  # noqa: BLE001 - a broken token must not stop analysis
            _LOGGER.warning("GitHub credential unavailable (%s)", type(error).__name__)
            return None

    async def _request(
        self,
        path: str,
        *,
        credential: GitHubCredential | None,
        accept: str,
        etag: str | None,
        max_bytes: int,
        truncate: bool,
    ) -> _Response:
        url = f"{self._api_origin}{path}"
        response = await _get(
            url,
            credential=credential,
            accept=accept,
            etag=etag,
            max_bytes=max_bytes,
            truncate=truncate,
            timeout_seconds=self._timeout_seconds,
        )
        if response.status in _REDIRECT_STATUSES:
            target = _api_redirect(response.headers.get("location", ""))
            if target is None:
                return _Response(502, response.headers)
            response = await _get(
                target,
                credential=credential,
                accept=accept,
                etag=etag,
                max_bytes=max_bytes,
                truncate=truncate,
                timeout_seconds=self._timeout_seconds,
            )
            if response.status in _REDIRECT_STATUSES:
                return _Response(502, response.headers)
        return response

    async def _stats(
        self,
        ref: GitHubRepoRef,
        credential: GitHubCredential | None,
        etag: str | None,
    ) -> tuple[GitHubFetchResult, GitHubCredential | None]:
        key = credential.key if credential is not None else ANONYMOUS_CREDENTIAL_KEY
        if backoff_remaining(key) > 0:
            return GitHubFetchResult("rate_limited"), credential
        response = await self._request(
            ref.api_path,
            credential=credential,
            accept="application/vnd.github+json",
            etag=etag,
            max_bytes=MAX_REPOSITORY_JSON_BYTES,
            truncate=False,
        )
        _note_rate_limit(key, response.status, response.headers)
        if response.status == 401 and credential is not None:
            # An expired or revoked token must not hide public data: retry
            # once anonymously and keep using anonymous access for the README.
            _LOGGER.info("GitHub rejected the account token; retrying anonymously")
            return await self._stats(ref, None, etag)
        if response.status == 304:
            return GitHubFetchResult("not_modified", etag=etag), credential
        if response.status == 404:
            return GitHubFetchResult("not_found"), credential
        if response.status in _RATE_LIMIT_STATUSES:
            return GitHubFetchResult("rate_limited"), credential
        if response.status != 200:
            return GitHubFetchResult("failed"), credential
        try:
            snapshot = parse_repository(json.loads(response.body))
        except ValueError:
            snapshot = None
        if snapshot is None:
            return GitHubFetchResult("failed"), credential
        return (
            GitHubFetchResult(
                "ok",
                snapshot=snapshot,
                etag=valid_etag(response.headers.get("etag")),
            ),
            credential,
        )

    async def _readme(self, ref: GitHubRepoRef, credential: GitHubCredential | None) -> str:
        key = credential.key if credential is not None else ANONYMOUS_CREDENTIAL_KEY
        if backoff_remaining(key) > 0:
            return ""
        response = await self._request(
            f"{ref.api_path}/readme",
            credential=credential,
            accept="application/vnd.github.raw",
            etag=None,
            max_bytes=README_EXCERPT_BYTES,
            truncate=True,
        )
        _note_rate_limit(key, response.status, response.headers)
        if response.status != 200:
            return ""
        return readme_excerpt(response.body)

    async def fetch(
        self,
        ref: GitHubRepoRef,
        *,
        credential: GitHubCredential | None,
        etag: str | None,
        include_stats: bool,
        include_readme: bool,
    ) -> GitHubFetchResult:
        result = GitHubFetchResult("skipped")
        active_credential = credential
        try:
            if include_stats:
                result, active_credential = await self._stats(ref, credential, valid_etag(etag))
        except Exception as error:  # noqa: BLE001 - GitHub is optional evidence
            _LOGGER.warning("GitHub repository request failed (%s)", type(error).__name__)
            return GitHubFetchResult("failed")
        if not include_readme or result.status in {"not_found", "rate_limited", "failed"}:
            return result
        try:
            excerpt = await self._readme(ref, active_credential)
        except Exception as error:  # noqa: BLE001 - README is optional evidence
            _LOGGER.warning("GitHub README request failed (%s)", type(error).__name__)
            excerpt = ""
        return GitHubFetchResult(
            result.status,
            snapshot=result.snapshot,
            etag=result.etag,
            readme_excerpt=excerpt,
        )


__all__ = [
    "ANONYMOUS_CREDENTIAL_KEY",
    "GITHUB_API_ORIGIN",
    "README_EXCERPT_BYTES",
    "STATS_TTL",
    "CredentialResolver",
    "GitHubCredential",
    "GitHubFetchResult",
    "GitHubRepoRef",
    "GitHubRepoSnapshot",
    "GitHubRepositoryEnricher",
    "RepositoryEnricher",
    "backoff_remaining",
    "github_meta_update",
    "github_repo_ref",
    "parse_repository",
    "readme_excerpt",
    "repository_evidence",
    "reset_rate_limit_state",
    "stats_are_fresh",
    "stored_etag",
    "stored_stats",
    "valid_etag",
]
