"""Migration 20261021_0020: resource kinds, usage signals and resurfacing."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from alembic import command
from webhub.db.database import DATABASE_SCHEMA_HEADS
from webhub.db.migrations import create_alembic_config

PREVIOUS = "20260731_0019"
TIMESTAMP = "2026-07-31 06:00:00.000000"
USER_ID = "00000000-0000-0000-0000-0000000000a1"
CATEGORY_ID = "00000000-0000-0000-0000-0000000000c1"
NEW_COLUMNS = {
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
}
NEW_INDEXES = {"ix_sites_user_kind_created_id", "ix_sites_user_last_opened_id"}
# Fixed expectations (not recomputed from ``webhub.library.kinds``): the
# migration carries its own frozen copy of the rules.
CLASSIFIED_SITES = {
    "site-repo": (
        "LangGraph",
        "https://github.com/langchain-ai/langgraph",
        "repo",
        {"host": "github.com", "owner": "langchain-ai", "repo": "langgraph"},
    ),
    "site-dataset": (
        "GSM8K",
        "https://huggingface.co/datasets/openai/gsm8k",
        "repo",
        {"host": "huggingface.co", "owner": "openai", "repo": "gsm8k", "repo_type": "dataset"},
    ),
    "site-gitlab": (
        "GitLab project",
        "https://gitlab.com/group/sub/project/-/tree/main",
        "repo",
        {"host": "gitlab.com", "owner": "group/sub", "repo": "project"},
    ),
    "site-issue": ("Issue", "https://github.com/owner/repo/issues/12", "article", {}),
    "site-video": ("Talk", "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "video", {}),
    "site-bilibili": ("B 站", "https://www.bilibili.com/video/BV1xx411c7mD", "video", {}),
    "site-post": ("Post", "https://blog.example/blog/post-1", "article", {}),
    "site-pdf": ("Paper", "https://papers.example/attention.pdf", "article", {}),
    "site-wechat": ("公众号", "https://mp.weixin.qq.com/s/AbCdEf", "article", {}),
    "site-home": ("Home", "https://example.com/", "website", {}),
    "site-tool": ("Tool", "https://tool.example/pricing", "website", {}),
}
# More rows than one backfill chunk (500) so the keyset loop is exercised.
BULK_SITE_COUNT = 1_203


def _bulk_site_id(index: int) -> str:
    return f"site-bulk-{index:05d}"


def _seed_at_previous_revision(database_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO users(id, username, display_name, password_hash, is_active, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (USER_ID, "migrated", "Migrated", "hash", 1, TIMESTAMP, TIMESTAMP),
        )
        connection.execute(
            "INSERT INTO categories(id, user_id, name, normalized_name, is_default, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (CATEGORY_ID, USER_ID, "开发", "开发", 1, TIMESTAMP, TIMESTAMP),
        )
        rows = [
            (site_id, name, url)
            for site_id, (name, url, _, _) in CLASSIFIED_SITES.items()
        ] + [
            (_bulk_site_id(index), f"Bulk {index}", f"https://bulk.example/{index}/item")
            for index in range(BULK_SITE_COUNT)
        ]
        connection.executemany(
            "INSERT INTO sites(id, user_id, category_id, position, name, normalized_name, "
            "original_url, identity_url, description, pinned, source, analysis_status, "
            "version, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', 0, 'manual', 'not_analyzed', 3, ?, ?)",
            [
                (
                    site_id,
                    USER_ID,
                    CATEGORY_ID,
                    position,
                    name,
                    name.casefold(),
                    url,
                    url,
                    TIMESTAMP,
                    TIMESTAMP,
                )
                for position, (site_id, name, url) in enumerate(rows)
            ],
        )
        connection.commit()


def _columns(connection: sqlite3.Connection) -> set[str]:
    return {str(row[1]) for row in connection.execute("PRAGMA table_info(sites)")}


def _indexes(connection: sqlite3.Connection) -> set[str]:
    return {str(row[1]) for row in connection.execute("PRAGMA index_list(sites)")}


def _trigger_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = ?",
            (name,),
        ).fetchone()
        is not None
    )


def _edit_snapshot(connection: sqlite3.Connection) -> dict[str, tuple[object, ...]]:
    return {
        str(row[0]): tuple(row[1:])
        for row in connection.execute(
            "SELECT id, name, identity_url, version, updated_at, created_at FROM sites"
        )
    }


def _assert_backfilled(connection: sqlite3.Connection) -> None:
    rows = {
        str(row[0]): row[1:]
        for row in connection.execute(
            "SELECT id, kind, kind_source, meta_json, open_count, last_opened_at, seen_at, "
            "starred_at, resurface_due_at, resurface_count, resurface_muted FROM sites"
        )
    }
    for site_id, (_, _, kind, meta) in CLASSIFIED_SITES.items():
        stored_kind, kind_source, meta_json, *usage = rows[site_id]
        assert (stored_kind, kind_source) == (kind, "auto"), site_id
        assert json.loads(meta_json) == meta, site_id
        assert usage == [0, None, None, None, None, 0, 0], site_id
    bulk = [rows[_bulk_site_id(index)] for index in range(BULK_SITE_COUNT)]
    assert {(row[0], row[1], row[2]) for row in bulk} == {("article", "auto", "{}")}


def test_upgrade_backfills_kinds_without_touching_edit_state_and_downgrades(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "engagement.sqlite3"
    config = create_alembic_config(f"sqlite+aiosqlite:///{database_path.as_posix()}")
    command.upgrade(config, PREVIOUS)
    _seed_at_previous_revision(database_path)
    with sqlite3.connect(database_path) as connection:
        before = _edit_snapshot(connection)
        assert not NEW_COLUMNS & _columns(connection)

    command.upgrade(config, "head")
    command.check(config)
    with sqlite3.connect(database_path) as connection:
        assert _columns(connection) >= NEW_COLUMNS
        assert _indexes(connection) >= NEW_INDEXES
        assert _trigger_exists(connection, "categories_search_rename")
        # Backfill is derived data: no version bump, no "recently edited".
        assert _edit_snapshot(connection) == before
        _assert_backfilled(connection)
        # The FTS index still answers for migrated rows.
        assert connection.execute(
            "SELECT site_id FROM site_search WHERE site_search MATCH 'langgraph'"
        ).fetchall() == [("site-repo",)]
        # Column-level CHECK constraints survive on the live table.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE sites SET kind = 'podcast' WHERE id = 'site-home'")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE sites SET kind_source = 'robot' WHERE id = 'site-home'")
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert version is not None
    assert {version[0]} == DATABASE_SCHEMA_HEADS

    command.downgrade(config, PREVIOUS)
    with sqlite3.connect(database_path) as connection:
        assert not NEW_COLUMNS & _columns(connection)
        assert not NEW_INDEXES & _indexes(connection)
        assert _edit_snapshot(connection) == before
        # The detached cross-table trigger is back and still maintains search.
        assert _trigger_exists(connection, "categories_search_rename")
        connection.execute("UPDATE categories SET name = '工具' WHERE id = ?", (CATEGORY_ID,))
        assert connection.execute(
            "SELECT DISTINCT category_name FROM site_search WHERE user_id = ?",
            (USER_ID,),
        ).fetchall() == [("工具",)]
        connection.commit()

    command.upgrade(config, "head")
    command.check(config)
    with sqlite3.connect(database_path) as connection:
        _assert_backfilled(connection)


def test_interrupted_upgrade_can_simply_be_rerun(tmp_path: Path) -> None:
    database_path = tmp_path / "interrupted.sqlite3"
    config = create_alembic_config(f"sqlite+aiosqlite:///{database_path.as_posix()}")
    command.upgrade(config, PREVIOUS)
    _seed_at_previous_revision(database_path)
    # SQLite DDL is not transactional: simulate a crash after the first ALTER.
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "ALTER TABLE sites ADD COLUMN kind TEXT NOT NULL DEFAULT 'website' "
            "CONSTRAINT ck_sites_valid_kind "
            "CHECK (kind IN ('repo', 'article', 'video', 'website'))"
        )
        connection.commit()

    command.upgrade(config, "head")
    command.check(config)
    with sqlite3.connect(database_path) as connection:
        assert _columns(connection) >= NEW_COLUMNS
        _assert_backfilled(connection)
