from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from alembic import command
from webhub.db.migrations import create_alembic_config


def test_site_intents_migration_round_trip_preserves_existing_site(tmp_path: Path) -> None:
    path = tmp_path / "main.sqlite3"
    config = create_alembic_config(f"sqlite+aiosqlite:///{path}")
    command.upgrade(config, "20261022_0024")
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            INSERT INTO users(id, username, display_name, password_hash, is_active,
                              created_at, updated_at)
            VALUES ('u', 'alice', 'Alice', 'test', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
            INSERT INTO categories(id, user_id, name, normalized_name,
                                   is_default, created_at, updated_at)
            VALUES ('c', 'u', '收藏', '收藏', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
            INSERT INTO sites(id, user_id, category_id, name, normalized_name,
                              original_url, identity_url, description, position, pinned,
                              analysis_status, version, source, created_at, updated_at)
            VALUES ('s', 'u', 'c', 'Existing', 'existing', 'https://example.test/',
                    'https://example.test/', '', 0, 0, 'not_analyzed', 7, 'manual',
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
        """)
        before = connection.execute("SELECT * FROM sites WHERE id='s'").fetchone()
    command.upgrade(config, "head")
    command.check(config)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("""INSERT INTO site_analysis_intents
            (user_id, site_id, next_attempt_at, created_at, updated_at)
            VALUES ('u', 's', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""")
        assert connection.execute(
            "SELECT intent, attempts FROM site_analysis_intents"
        ).fetchone() == (
            "site_enrichment",
            0,
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE site_analysis_intents SET intent='unknown'")
        connection.rollback()
        indexes = list(connection.execute("PRAGMA index_list(site_analysis_intents)"))
        assert any(row[1] == "ix_site_analysis_intents_next_attempt_at" for row in indexes)
    command.downgrade(config, "20261022_0024")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT * FROM sites WHERE id='s'").fetchone() == before
        assert not connection.execute(
            "SELECT name FROM sqlite_master WHERE name='site_analysis_intents'",
        ).fetchall()
    command.upgrade(config, "head")
    command.check(config)
