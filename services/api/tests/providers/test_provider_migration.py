from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from alembic import command
from webhub.db.migrations import create_alembic_config


def test_provider_migration_preserves_one_enabled_config_and_adds_unique_guard(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "provider-migration.sqlite3"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    config = create_alembic_config(database_url)
    command.upgrade(config, "20260726_0004")

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO users(id, username, display_name, password_hash, is_active, "
            "created_at, updated_at) VALUES "
            "('user', 'provider-user', 'Provider User', 'hash', 1, ?, ?)",
            ("2026-07-26 00:00:00+00:00", "2026-07-26 00:00:00+00:00"),
        )
        for config_id, updated_at in (
            ("older", "2026-07-26 01:00:00+00:00"),
            ("newer", "2026-07-26 02:00:00+00:00"),
        ):
            connection.execute(
                "INSERT INTO provider_configs("
                "id, user_id, kind, provider, display_name, base_url, model_name, "
                "secret_ciphertext, secret_nonce, key_version, enabled, config_json, "
                "created_at, updated_at"
                ") VALUES (?, 'user', 'model', 'ollama', ?, NULL, 'model', "
                "NULL, NULL, 1, 1, '{}', ?, ?)",
                (config_id, config_id, updated_at, updated_at),
            )
        connection.commit()

    command.upgrade(config, "20260726_0005")
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT id, enabled, version FROM provider_configs ORDER BY id"
        ).fetchall()
        assert rows == [("newer", 1, 1), ("older", 0, 1)]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE provider_configs SET enabled = 1 WHERE id = 'older'")

    command.downgrade(config, "20260726_0004")
    with sqlite3.connect(database_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(provider_configs)")}
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(provider_configs)")}
    assert "version" not in columns
    assert "uq_provider_configs_enabled_per_user_kind" not in indexes
