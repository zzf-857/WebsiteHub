"""Deterministic resource-kind rules for one saved URL.

This module is deliberately pure: no database, no network, no settings. The
same functions run when a site is inserted (``Site.kind`` / ``Site.meta_json``
column defaults) and when an automatic kind is re-derived after a URL edit, so
every creation path classifies one URL the same way. Migration
``20261021_0020`` carries a frozen copy of these rules for its backfill, as
historical migrations must not import application code that may change later.

The rules are a conservative ladder, evaluated in order:

1. code forges (GitHub / GitLab / Gitee / Codeberg / Hugging Face): a repository
   path, including in-repository paths such as ``/tree/`` and ``/blob/``, is a
   ``repo``; its issue / pull / merge-request / discussion pages are ``article``;
   the forge's own product and listing pages are ``website``;
2. video hosts (YouTube, youtu.be, Bilibili, b23.tv, Vimeo): a playable path is
   ``video``; a post on those hosts is ``article``; channels and the like are
   ``website``;
3. post / article path shapes, PDFs and paths at least two segments deep are
   ``article``;
4. everything else, notably the root and a single short segment, is ``website``.

Any parse failure degrades to ``website``; nothing here raises on user input.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qs, unquote, urlsplit

SiteKind = Literal["repo", "article", "video", "website"]
KindSource = Literal["auto", "manual"]
SITE_KINDS: tuple[SiteKind, ...] = ("repo", "article", "video", "website")
KIND_SOURCES: tuple[KindSource, ...] = ("auto", "manual")
DEFAULT_KIND: SiteKind = "website"

# Owner / repository path component on a forge ("." and ".." are never names).
_FORGE_NAME = re.compile(r"^(?!\.{1,2}$)[A-Za-z0-9._-]{1,100}$")

# First path segments that are a forge's own product, listing or account pages,
# never a repository owner.
_FLAT_FORGE_RESERVED = {
    "github.com": frozenset(
        {
            "about", "account", "apps", "codespaces", "collections", "contact",
            "copilot", "customer-stories", "dashboard", "discussions", "education",
            "enterprise", "events", "explore", "features", "github-copilot", "home",
            "issues", "join", "login", "logout", "marketplace", "mobile", "new",
            "notifications", "organizations", "orgs", "pricing", "pulls", "readme",
            "search", "security", "sessions", "settings", "signup", "site", "solutions",
            "sponsors", "stars", "team", "topics", "trending", "watching",
        }
    ),
    "gitee.com": frozenset(
        {
            "about", "education", "enterprises", "explore", "features", "gvp", "help",
            "login", "organizations", "search", "signup",
        }
    ),
    "codeberg.org": frozenset(
        {"-", "admin", "api", "explore", "notifications", "org", "repo", "user"}
    ),
}
_GITLAB_RESERVED = frozenset(
    {"-", "admin", "dashboard", "explore", "groups", "help", "projects", "search", "users"}
)
# Old-style GitLab project links have no ``/-/`` separator; the first of these
# sections (after at least ``group/project``) ends the project path.
_GITLAB_PROJECT_SECTIONS = frozenset(
    {
        "blame", "blob", "branches", "commit", "commits", "compare", "issues",
        "merge_requests", "pipelines", "raw", "releases", "tags", "tree", "wikis",
        "work_items",
    }
)
_HUGGINGFACE_RESERVED = frozenset(
    {
        "chat", "collections", "enterprise", "inference-endpoints", "join", "login",
        "models", "new", "organizations", "pricing", "search", "settings", "tasks",
        "welcome",
    }
)
# Hugging Face editorial sections, classified by the generic article rules.
_HUGGINGFACE_CONTENT = frozenset({"blog", "docs", "learn", "papers", "posts"})
_HUGGINGFACE_NAMESPACES = {"datasets": "dataset", "spaces": "space"}

# In-repository sections whose item pages are discussions, not code.
_ITEM_SECTIONS = {
    "github.com": frozenset({"issues", "pull", "pulls", "discussions"}),
    "gitee.com": frozenset({"issues", "pulls"}),
    "codeberg.org": frozenset({"issues", "pulls"}),
    "gitlab.com": frozenset({"issues", "merge_requests", "work_items"}),
    "huggingface.co": frozenset({"discussions"}),
}
_FORGE_HOSTS = frozenset({*_FLAT_FORGE_RESERVED, "gitlab.com", "huggingface.co"})
_HOST_ALIASES = {"hf.co": "huggingface.co"}

# A marker segment followed by at least one more segment is a post/article.
_ARTICLE_MARKERS = frozenset(
    {
        "answer", "answers", "article", "articles", "blog", "blogs", "comments",
        "p", "post", "posts", "question", "questions", "status", "statuses", "t",
        "thread", "threads",
    }
)
# Listing / profile sections. Without an article marker they stay ``website``.
_LISTING_SECTIONS = frozenset(
    {
        "author", "authors", "c", "categories", "category", "channel", "channels",
        "go", "member", "members", "people", "profile", "r", "search", "tag", "tags",
        "topics", "u", "user", "users",
    }
)
_DOCUMENT_SUFFIXES = (".md", ".markdown", ".rst", ".txt")
_PAGE_SUFFIXES = (".html", ".htm", ".shtml", ".php", ".aspx")
_LOCALE = re.compile(r"^([a-z]{2})(?:[-_][a-z]{2,4})?$")
_LOCALE_LANGUAGES = frozenset(
    {
        "ar", "cs", "da", "de", "el", "en", "es", "fi", "fr", "he", "hi", "hu", "id",
        "it", "ja", "ko", "nl", "no", "pl", "pt", "ro", "ru", "sv", "th", "tr", "uk",
        "vi", "zh",
    }
)
_SLUG_WORD_SEPARATOR = re.compile(r"[-_+.]+")
# A single path segment of up to two words / 40 characters is a page of a site
# ("/about", "/pricing", "/getting-started"); a longer slug is a post title.
_SHORT_SEGMENT_MAX_WORDS = 2
_SHORT_SEGMENT_MAX_LENGTH = 40


@dataclass(frozen=True, slots=True)
class _ForgeMatch:
    host: str
    owner: str
    repo: str
    repo_type: str | None
    is_item_page: bool


def _split(url: object) -> tuple[str, list[str], str] | None:
    """Return ``(host, decoded path segments, query)`` or ``None``."""

    if not isinstance(url, str):
        return None
    candidate = url.strip()
    if not candidate:
        return None
    if "://" not in candidate:
        candidate = f"https://{candidate.lstrip('/')}"
    try:
        parts = urlsplit(candidate)
        host = (parts.hostname or "").rstrip(".").casefold()
    except (ValueError, UnicodeError):
        return None
    if not host:
        return None
    for prefix in ("www.", "m.", "mobile."):
        if host.startswith(prefix) and host.count(".") >= 2:
            host = host[len(prefix) :]
            break
    host = _HOST_ALIASES.get(host, host)
    segments = [unquote(segment) for segment in parts.path.split("/") if segment]
    return host, segments, parts.query


def _valid_names(*names: str) -> bool:
    return all(_FORGE_NAME.match(name) for name in names)


def _strip_git_suffix(name: str) -> str:
    return name[: -len(".git")] if name.casefold().endswith(".git") and len(name) > 4 else name


def _is_item_page(host: str, rest: list[str]) -> bool:
    return len(rest) >= 2 and rest[0].casefold() in _ITEM_SECTIONS[host]


def _flat_forge(host: str, segments: list[str]) -> _ForgeMatch | None:
    if len(segments) < 2 or segments[0].casefold() in _FLAT_FORGE_RESERVED[host]:
        return None
    owner, repo = segments[0], _strip_git_suffix(segments[1])
    if not _valid_names(owner, repo):
        return None
    return _ForgeMatch(host, owner, repo, None, _is_item_page(host, segments[2:]))


def _gitlab(segments: list[str]) -> _ForgeMatch | None:
    if not segments or segments[0].casefold() in _GITLAB_RESERVED:
        return None
    if "-" in segments:
        separator = segments.index("-")
        project, rest = segments[:separator], segments[separator + 1 :]
    else:
        section = next(
            (
                index
                for index, segment in enumerate(segments)
                if index >= 2 and segment.casefold() in _GITLAB_PROJECT_SECTIONS
            ),
            len(segments),
        )
        project, rest = segments[:section], segments[section:]
    if not 2 <= len(project) <= 8:
        return None
    project = [*project[:-1], _strip_git_suffix(project[-1])]
    if not _valid_names(*project):
        return None
    return _ForgeMatch(
        "gitlab.com",
        "/".join(project[:-1]),
        project[-1],
        None,
        _is_item_page("gitlab.com", rest),
    )


def _huggingface(segments: list[str]) -> _ForgeMatch | None:
    if not segments:
        return None
    first = segments[0].casefold()
    repo_type = "model"
    if first in _HUGGINGFACE_NAMESPACES:
        repo_type = _HUGGINGFACE_NAMESPACES[first]
        segments = segments[1:]
    elif first in _HUGGINGFACE_RESERVED or first in _HUGGINGFACE_CONTENT:
        return None
    if len(segments) < 2 or not _valid_names(segments[0], segments[1]):
        return None
    return _ForgeMatch(
        "huggingface.co",
        segments[0],
        segments[1],
        repo_type,
        _is_item_page("huggingface.co", segments[2:]),
    )


def _forge_match(host: str, segments: list[str]) -> _ForgeMatch | None:
    if host in _FLAT_FORGE_RESERVED:
        return _flat_forge(host, segments)
    if host == "gitlab.com":
        return _gitlab(segments)
    if host == "huggingface.co":
        return _huggingface(segments)
    return None


def _host_is(host: str, domain: str) -> bool:
    return host == domain or host.endswith(f".{domain}")


def _video_family(host: str) -> str | None:
    if _host_is(host, "youtube.com") or _host_is(host, "youtube-nocookie.com"):
        return "youtube"
    if host == "youtu.be":
        return "youtu.be"
    if _host_is(host, "bilibili.com"):
        return "bilibili"
    if host == "b23.tv":
        return "b23.tv"
    if _host_is(host, "vimeo.com"):
        return "vimeo"
    return None


def _is_playable(family: str, host: str, lowered: list[str], query: str) -> bool:
    first = lowered[0] if lowered else ""
    if family == "youtube":
        if first == "watch":
            return "v" in parse_qs(query)
        if first == "playlist":
            return "list" in parse_qs(query)
        return first in {"shorts", "live", "embed", "v"} and len(lowered) >= 2
    if family in {"youtu.be", "b23.tv"}:
        return bool(lowered)
    if family == "bilibili":
        if host.startswith("live."):
            return bool(lowered) and first.isdigit()
        return first in {"video", "bangumi", "list", "medialist"} and len(lowered) >= 2
    # vimeo: every clip URL carries its numeric id as a path segment.
    return any(segment.isdigit() for segment in lowered)


def _has_article_marker(lowered: list[str]) -> bool:
    return any(segment in _ARTICLE_MARKERS for segment in lowered[:-1])


def _strip_locale(lowered: list[str]) -> list[str]:
    if lowered and (match := _LOCALE.match(lowered[0])) and match[1] in _LOCALE_LANGUAGES:
        return lowered[1:]
    return lowered


def _is_long_slug(segment: str) -> bool:
    if segment.startswith("@"):
        return False
    if segment.endswith(_DOCUMENT_SUFFIXES):
        return True
    for suffix in _PAGE_SUFFIXES:
        if segment.endswith(suffix):
            segment = segment[: -len(suffix)]
            break
    words = [word for word in _SLUG_WORD_SEPARATOR.split(segment) if word]
    return len(words) > _SHORT_SEGMENT_MAX_WORDS or len(segment) > _SHORT_SEGMENT_MAX_LENGTH


def _is_article(host: str, segments: list[str], query: str) -> bool:
    lowered = _strip_locale([segment.casefold() for segment in segments])
    if lowered and lowered[-1].endswith(".pdf"):
        return True
    params = parse_qs(query)
    if host == "news.ycombinator.com":
        return lowered == ["item"] and "id" in params
    if host == "mp.weixin.qq.com":
        return bool(lowered) and lowered[0] == "s"
    if not lowered:
        # WordPress permalinks without rewrites: https://example.com/?p=123
        return any(value.isdigit() for value in params.get("p", ()))
    if _has_article_marker(lowered):
        return True
    if lowered[0] in _LISTING_SECTIONS:
        return False
    return len(lowered) >= 2 or _is_long_slug(lowered[0])


def infer_kind(url: str) -> SiteKind:
    """Classify one URL into a resource kind; never raises on bad input."""

    split = _split(url)
    if split is None:
        return DEFAULT_KIND
    host, segments, query = split
    if host in _FORGE_HOSTS:
        forge = _forge_match(host, segments)
        if forge is not None:
            return "article" if forge.is_item_page else "repo"
        if (
            host == "huggingface.co"
            and segments
            and segments[0].casefold() in _HUGGINGFACE_CONTENT
            and _is_article(host, segments, query)
        ):
            return "article"
        return "website"
    lowered = [segment.casefold() for segment in segments]
    family = _video_family(host)
    if family is not None:
        if _is_playable(family, host, lowered, query):
            return "video"
        is_post = _has_article_marker(lowered) or (
            family == "bilibili" and len(lowered) >= 2 and lowered[0] == "read"
        )
        return "article" if is_post else "website"
    return "article" if _is_article(host, segments, query) else "website"


def repo_meta(url: str) -> dict[str, str] | None:
    """Parse ``host`` / ``owner`` / ``repo`` for a URL inside a forge repository.

    Item pages (issues, pulls, discussions) still belong to their repository
    and parse; callers decide whether to store the result for that kind.
    Hugging Face results also carry ``repo_type`` (model, dataset or space);
    GitLab owners may be nested groups (``group/subgroup``).
    """

    split = _split(url)
    if split is None:
        return None
    host, segments, _ = split
    forge = _forge_match(host, segments)
    if forge is None:
        return None
    meta = {"host": forge.host, "owner": forge.owner, "repo": forge.repo}
    if forge.repo_type is not None:
        meta["repo_type"] = forge.repo_type
    return meta


def kind_meta(kind: str, url: str) -> dict[str, str]:
    """URL-derived metadata stored with a kind; empty when nothing applies."""

    if kind != "repo":
        return {}
    return repo_meta(url) or {}


def kind_meta_json(kind: str, url: str) -> str:
    """Canonical ``sites.meta_json`` text for ``kind_meta`` (stable key order)."""

    return json.dumps(
        kind_meta(kind, url),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def kind_columns(url: str) -> tuple[SiteKind, str]:
    """``(kind, meta_json)`` for a site whose kind is still automatic."""

    kind = infer_kind(url)
    return kind, kind_meta_json(kind, url)
