"""Add resource kinds, engagement signals and resurfacing state to sites.

Revision ID: 20261021_0020
Revises: 20260731_0019
Create Date: 2026-10-21
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from urllib.parse import parse_qs, unquote, urlsplit

import sqlalchemy as sa

from alembic import op

revision: str = "20261021_0020"
down_revision: str | None = "20260731_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Same guarded native-ALTER pattern as 0010 / 0012 / 0019: never let Alembic's
# batch mode rebuild `sites` (its FTS triggers would fire against a renamed
# table), and detach the one cross-table trigger only while dropping columns.
_CATEGORIES_SEARCH_RENAME = """
CREATE TRIGGER categories_search_rename AFTER UPDATE OF name ON categories BEGIN
    UPDATE site_search
    SET category_name = NEW.name
    WHERE user_id = NEW.user_id
      AND site_id IN (
          SELECT id FROM sites
          WHERE user_id = NEW.user_id AND category_id = NEW.id
      );
END
"""

# SQLite accepts a column-level CHECK on ADD COLUMN (validated against existing
# rows) and drops it together with the column. A table-level CHECK would need
# the forbidden rebuild. Alembic's add_column does not render inline column
# constraints, hence raw DDL for these two.
_CONSTRAINED_COLUMNS = {
    "kind": (
        "ALTER TABLE sites ADD COLUMN kind TEXT NOT NULL DEFAULT 'website' "
        "CONSTRAINT ck_sites_valid_kind "
        "CHECK (kind IN ('repo', 'article', 'video', 'website'))"
    ),
    "kind_source": (
        "ALTER TABLE sites ADD COLUMN kind_source TEXT NOT NULL DEFAULT 'auto' "
        "CONSTRAINT ck_sites_valid_kind_source "
        "CHECK (kind_source IN ('auto', 'manual'))"
    ),
}


def _plain_columns() -> list[sa.Column]:
    return [
        sa.Column("open_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("starred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resurface_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resurface_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("resurface_muted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("meta_json", sa.Text(), nullable=False, server_default="{}"),
    ]


_ADDED_COLUMNS = (
    "kind",
    "kind_source",
    "open_count",
    "last_opened_at",
    "seen_at",
    "starred_at",
    "resurface_due_at",
    "resurface_count",
    "resurface_muted",
    "meta_json",
)
_INDEXES = {
    # kind filter with created/id keyset order, and the per-kind overview scan.
    "ix_sites_user_kind_created_id": ["user_id", "kind", "created_at", "id"],
    # Rediscover candidates (never opened, rotated by id), state=opened /
    # unopened filters and sort=opened.
    "ix_sites_user_last_opened_id": ["user_id", "last_opened_at", "id"],
}
_BACKFILL_CHUNK = 500


# ── Frozen copy of ``webhub.library.kinds`` as of 2026-10-21 ──────────────
#
# Historical migrations must not import application code whose behaviour can
# change after they ship (see 20260727_0009). This is a behaviour-identical
# snapshot of the URL rules used for newly saved sites on the day this revision
# shipped; later rule changes apply only to new or retargeted sites.

_FORGE_NAME = re.compile(r"^(?!\.{1,2}$)[A-Za-z0-9._-]{1,100}$")
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
_HUGGINGFACE_CONTENT = frozenset({"blog", "docs", "learn", "papers", "posts"})
_HUGGINGFACE_NAMESPACES = {"datasets": "dataset", "spaces": "space"}
_ITEM_SECTIONS = {
    "github.com": frozenset({"issues", "pull", "pulls", "discussions"}),
    "gitee.com": frozenset({"issues", "pulls"}),
    "codeberg.org": frozenset({"issues", "pulls"}),
    "gitlab.com": frozenset({"issues", "merge_requests", "work_items"}),
    "huggingface.co": frozenset({"discussions"}),
}
_FORGE_HOSTS = frozenset({*_FLAT_FORGE_RESERVED, "gitlab.com", "huggingface.co"})
_HOST_ALIASES = {"hf.co": "huggingface.co"}
_ARTICLE_MARKERS = frozenset(
    {
        "answer", "answers", "article", "articles", "blog", "blogs", "comments",
        "p", "post", "posts", "question", "questions", "status", "statuses", "t",
        "thread", "threads",
    }
)
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


def _split(url: object) -> tuple[str, list[str], str] | None:
    if not isinstance(url, str) or not url.strip():
        return None
    candidate = url.strip()
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
    return host, [unquote(part) for part in parts.path.split("/") if part], parts.query


def _valid_names(*names: str) -> bool:
    return all(_FORGE_NAME.match(name) for name in names)


def _strip_git_suffix(name: str) -> str:
    return name[: -len(".git")] if name.casefold().endswith(".git") and len(name) > 4 else name


def _is_item_page(host: str, rest: list[str]) -> bool:
    return len(rest) >= 2 and rest[0].casefold() in _ITEM_SECTIONS[host]


def _forge_match(host: str, segments: list[str]) -> tuple[dict[str, str], bool] | None:
    """``(repo meta, is item page)`` for a repository path on a forge."""

    repo_type: str | None = None
    if host in _FLAT_FORGE_RESERVED:
        if len(segments) < 2 or segments[0].casefold() in _FLAT_FORGE_RESERVED[host]:
            return None
        owner, repo, rest = segments[0], _strip_git_suffix(segments[1]), segments[2:]
        if not _valid_names(owner, repo):
            return None
    elif host == "gitlab.com":
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
        owner, repo = "/".join(project[:-1]), project[-1]
    elif host == "huggingface.co":
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
        owner, repo, rest = segments[0], segments[1], segments[2:]
    else:
        return None
    meta = {"host": host, "owner": owner, "repo": repo}
    if repo_type is not None:
        meta["repo_type"] = repo_type
    return meta, _is_item_page(host, rest)


def _host_is(host: str, domain: str) -> bool:
    return host == domain or host.endswith(f".{domain}")


def _video_family(host: str) -> str | None:
    if _host_is(host, "youtube.com") or _host_is(host, "youtube-nocookie.com"):
        return "youtube"
    if host in {"youtu.be", "b23.tv"}:
        return host
    if _host_is(host, "bilibili.com"):
        return "bilibili"
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
    return any(segment.isdigit() for segment in lowered)


def _has_article_marker(lowered: list[str]) -> bool:
    return any(segment in _ARTICLE_MARKERS for segment in lowered[:-1])


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
    return len(words) > 2 or len(segment) > 40


def _is_article(host: str, segments: list[str], query: str) -> bool:
    lowered = [segment.casefold() for segment in segments]
    if lowered and (match := _LOCALE.match(lowered[0])) and match[1] in _LOCALE_LANGUAGES:
        lowered = lowered[1:]
    if lowered and lowered[-1].endswith(".pdf"):
        return True
    params = parse_qs(query)
    if host == "news.ycombinator.com":
        return lowered == ["item"] and "id" in params
    if host == "mp.weixin.qq.com":
        return bool(lowered) and lowered[0] == "s"
    if not lowered:
        return any(value.isdigit() for value in params.get("p", ()))
    if _has_article_marker(lowered):
        return True
    if lowered[0] in _LISTING_SECTIONS:
        return False
    return len(lowered) >= 2 or _is_long_slug(lowered[0])


def _kind_columns(url: object) -> tuple[str, str]:
    """``(kind, meta_json)`` exactly as ``kinds.kind_columns`` computed it."""

    split = _split(url)
    if split is None:
        return "website", "{}"
    host, segments, query = split
    kind = "website"
    meta: dict[str, str] = {}
    if host in _FORGE_HOSTS:
        forge = _forge_match(host, segments)
        if forge is not None:
            kind = "article" if forge[1] else "repo"
            meta = forge[0] if kind == "repo" else {}
        elif (
            host == "huggingface.co"
            and segments
            and segments[0].casefold() in _HUGGINGFACE_CONTENT
            and _is_article(host, segments, query)
        ):
            kind = "article"
    elif (family := _video_family(host)) is not None:
        lowered = [segment.casefold() for segment in segments]
        if _is_playable(family, host, lowered, query):
            kind = "video"
        elif _has_article_marker(lowered) or (
            family == "bilibili" and len(lowered) >= 2 and lowered[0] == "read"
        ):
            kind = "article"
    elif _is_article(host, segments, query):
        kind = "article"
    return kind, json.dumps(meta, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# ── Schema steps ──────────────────────────────────────────────────────────


def _site_columns() -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns("sites")}


def _site_indexes() -> set[str]:
    return {
        str(index["name"])
        for index in sa.inspect(op.get_bind()).get_indexes("sites")
        if index.get("name")
    }


def _backfill_kinds() -> None:
    """Classify every existing row with the URL rules new sites use.

    Keyset-chunked by id so a large library is never held in memory at once.
    Only rows whose result differs from the column defaults are written. The
    UPDATE touches neither `version` nor `updated_at`, and the FTS update
    trigger listens to other columns, so search and edit history stay as-is.
    On a fresh database `sites` is empty and this is a no-op.
    """

    bind = op.get_bind()
    select_chunk = sa.text(
        "SELECT id, identity_url FROM sites "
        "WHERE kind_source = 'auto' AND id > :after ORDER BY id LIMIT :limit"
    )
    update_row = sa.text("UPDATE sites SET kind = :kind, meta_json = :meta_json WHERE id = :id")
    after = ""
    while True:
        rows = bind.execute(select_chunk, {"after": after, "limit": _BACKFILL_CHUNK}).all()
        if not rows:
            return
        updates = []
        for site_id, identity_url in rows:
            kind, meta_json = _kind_columns(identity_url)
            if kind != "website" or meta_json != "{}":
                updates.append({"id": site_id, "kind": kind, "meta_json": meta_json})
        if updates:
            bind.execute(update_row, updates)
        after = str(rows[-1][0])


def upgrade() -> None:
    # SQLite DDL here is not transactional. Inspect before each step so an
    # interrupted upgrade can simply be re-run instead of needing a stamp.
    existing = _site_columns()
    for name, ddl in _CONSTRAINED_COLUMNS.items():
        if name not in existing:
            op.execute(ddl)
    for column in _plain_columns():
        if column.name not in existing:
            op.add_column("sites", column)

    _backfill_kinds()

    existing_indexes = _site_indexes()
    for index_name, columns in _INDEXES.items():
        if index_name not in existing_indexes:
            op.create_index(index_name, "sites", columns, unique=False)


def downgrade() -> None:
    # SQLite refuses to drop an indexed column, so the indexes go first.
    existing_indexes = _site_indexes()
    for index_name in reversed(list(_INDEXES)):
        if index_name in existing_indexes:
            op.drop_index(index_name, table_name="sites")
    # Native SQLite DROP COLUMN avoids Alembic batch rebuilding `sites`; the
    # cross-table category trigger must be detached during that ALTER.
    existing = _site_columns()
    op.execute("DROP TRIGGER IF EXISTS categories_search_rename")
    for name in reversed(_ADDED_COLUMNS):
        if name in existing:
            op.drop_column("sites", name)
    op.execute(_CATEGORIES_SEARCH_RENAME)
