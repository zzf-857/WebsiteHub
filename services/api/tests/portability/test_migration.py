from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from alembic import command
from webhub.db.migrations import create_alembic_config


def test_portability_migration_upgrade_downgrade_reupgrade(tmp_path: Path) -> None:
    path = tmp_path / "migration.sqlite3"
    config = create_alembic_config(f"sqlite+aiosqlite:///{path}")
    command.upgrade(config, "20261022_0024")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO users (id,username,display_name,password_hash,is_active,"
            "created_at,updated_at) VALUES ('u','alpha','Alpha','fake',1,CURRENT_TIMESTAMP,"
            "CURRENT_TIMESTAMP)"
        )
        connection.execute(
            "INSERT INTO categories (id,user_id,name,normalized_name,is_default,"
            "created_at,updated_at,icon) VALUES ('c','u','阅读','阅读',1,CURRENT_TIMESTAMP,"
            "CURRENT_TIMESTAMP,'BookOpen')"
        )
        connection.commit()
    command.upgrade(config, "head")
    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(portability_imports)")}
        assert columns == {
            "id",
            "user_id",
            "status",
            "payload_sha256",
            "payload_path",
            "summary_json",
            "result_json",
            "created_at",
            "expires_at",
            "applied_at",
        }
        assert connection.execute("SELECT name FROM categories").fetchall() == [("阅读",)]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO portability_imports VALUES "
                "('i','u','bad',?,'path','{}',NULL,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,NULL)",
                ("0" * 64,),
            )
        assert "ix_portability_imports_user_created" in {
            row[1] for row in connection.execute("PRAGMA index_list(portability_imports)")
        }
    command.downgrade(config, "20261022_0024")
    with sqlite3.connect(path) as connection:
        assert not connection.execute(
            "SELECT name FROM sqlite_master WHERE name='portability_imports'"
        ).fetchall()
        assert connection.execute("SELECT name FROM categories").fetchall() == [("阅读",)]
    command.upgrade(config, "head")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "20261023_0027",
        )
