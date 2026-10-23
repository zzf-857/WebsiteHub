"""Q51 迁移在已有订阅数据上升降级，不改收藏及其 FTS。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command
from webhub.db.database import DATABASE_SCHEMA_HEADS
from webhub.db.migrations import create_alembic_config

PREVIOUS = "20261022_0024"
STAMP = "2026-10-23 01:02:03.000000"


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def test_feed_media_upgrade_downgrade_and_reupgrade_preserves_existing_rows(tmp_path: Path) -> None:
    path = tmp_path / "feeds.sqlite3"
    config = create_alembic_config(f"sqlite+aiosqlite:///{path.as_posix()}")
    command.upgrade(config, PREVIOUS)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO users(id, username, display_name, password_hash, is_active, "
            "created_at, updated_at) VALUES ('user', 'reader', 'Reader', 'hash', 1, ?, ?)",
            (STAMP, STAMP),
        )
        connection.execute(
            "INSERT INTO feeds(id, user_id, url, title, kind, created_at) "
            "VALUES ('feed', 'user', 'https://feeds.example/rss', 'Source', 'rss', ?)",
            (STAMP,),
        )
        connection.execute(
            "INSERT INTO feed_items(id, feed_id, user_id, guid, title, summary, fetched_at) "
            "VALUES ('item', 'feed', 'user', 'guid', 'Item', 'Summary', ?)",
            (STAMP,),
        )
        connection.commit()
        before_feed = connection.execute("SELECT * FROM feeds").fetchall()
        before_item = connection.execute("SELECT * FROM feed_items").fetchall()
        site_schema = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE tbl_name = 'sites' OR type = 'trigger'"
        ).fetchall()

    command.upgrade(config, "head")
    command.check(config)
    with sqlite3.connect(path) as connection:
        assert {"purged_at", "image_url"} <= _columns(connection, "feed_items")
        assert "image_url" in _columns(connection, "feeds")
        assert connection.execute("SELECT purged_at, image_url FROM feed_items").fetchall() == [
            (None, None),
        ]
        assert connection.execute("SELECT image_url FROM feeds").fetchall() == [(None,)]
        assert [
            row[2] for row in connection.execute("PRAGMA index_info(ix_feed_items_user_purged)")
        ] == ["user_id", "purged_at"]
        assert {
            row[0] for row in connection.execute("SELECT version_num FROM alembic_version")
        } == (DATABASE_SCHEMA_HEADS)
        assert (
            connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE tbl_name = 'sites' OR type = 'trigger'"
            ).fetchall()
            == site_schema
        )

    command.downgrade(config, PREVIOUS)
    with sqlite3.connect(path) as connection:
        assert not {"purged_at", "image_url"} & _columns(connection, "feed_items")
        assert "image_url" not in _columns(connection, "feeds")
        assert "ix_feed_items_user_purged" not in {
            row[1] for row in connection.execute("PRAGMA index_list(feed_items)")
        }
        assert connection.execute("SELECT * FROM feeds").fetchall() == before_feed
        assert connection.execute("SELECT * FROM feed_items").fetchall() == before_item

    command.upgrade(config, "head")
    command.check(config)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT guid, purged_at, image_url FROM feed_items"
        ).fetchall() == [
            ("guid", None, None),
        ]
