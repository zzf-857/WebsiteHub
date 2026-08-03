from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from alembic import command
from webhub.db.migrations import create_alembic_config

SNAPSHOT_TABLES = {
    "site_similarity_scan_runs",
    "site_similarity_groups",
    "site_similarity_group_members",
    "site_similarity_decisions",
}
DECISION_MEMBER_TABLE = "site_similarity_decision_members"


def _tables(database_path: Path) -> set[str]:
    with sqlite3.connect(database_path) as connection:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }


def _seed_legacy_decisions(database_path: Path) -> None:
    timestamp = "2026-07-31 00:00:00+00:00"
    groups = (
        ("group-multi", "multi.example", 3, "site-a", 0),
        ("group-single", "single.example", 2, "site-d", 1),
        ("group-none", "none.example", 2, "site-f", 2),
    )
    members = (
        ("group-multi", "site-a", 0, 1),
        ("group-multi", "site-b", 1, 0),
        ("group-multi", "site-c", 2, 0),
        ("group-single", "site-d", 0, 1),
        ("group-single", "site-e", 1, 0),
        ("group-none", "site-f", 0, 1),
        ("group-none", "site-g", 1, 0),
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO users(id, username, display_name, password_hash, is_active, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("user", "similarity-user", "Similarity User", "hash", 1, timestamp, timestamp),
        )
        connection.execute(
            "INSERT INTO site_similarity_scan_runs("
            "id, user_id, status, ruleset_version, library_fingerprint, site_count, "
            "duplicate_group_count, same_site_group_count, member_count, version, "
            "result_json, created_at, updated_at, applied_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL)",
            (
                "run",
                "user",
                "ready",
                "library-site-similarity.v1",
                "a" * 64,
                7,
                3,
                0,
                7,
                3,
                timestamp,
                timestamp,
            ),
        )
        connection.executemany(
            "INSERT INTO site_similarity_groups("
            "id, user_id, run_id, site_key, kind, display_host, member_count, "
            "recommended_site_id, ordinal, created_at"
            ") VALUES (?, 'user', 'run', ?, 'duplicate', ?, ?, ?, ?, ?)",
            (
                (
                    group_id,
                    site_key,
                    site_key,
                    member_count,
                    recommended_site_id,
                    ordinal,
                    timestamp,
                )
                for group_id, site_key, member_count, recommended_site_id, ordinal in groups
            ),
        )
        connection.executemany(
            "INSERT INTO site_similarity_group_members("
            "user_id, run_id, group_id, site_id, expected_version, name, original_url, "
            "identity_url, summary, description, favicon_url, preview_url, category_id, "
            "category_name, category_is_default, category_icon, tags_json, pinned, source, "
            "analysis_status, site_created_at, site_updated_at, sort_order, is_recommended"
            ") VALUES ('user', 'run', ?, ?, 1, ?, ?, ?, '', '', NULL, NULL, 'category', "
            "'未分类', 1, 'Folder', '[]', 0, 'manual', 'pending', ?, ?, ?, ?)",
            (
                (
                    group_id,
                    site_id,
                    site_id,
                    f"https://{site_id}.example/resource",
                    f"https://{site_id}.example/resource",
                    timestamp,
                    timestamp,
                    sort_order,
                    is_recommended,
                )
                for group_id, site_id, sort_order, is_recommended in members
            ),
        )
        connection.executemany(
            "INSERT INTO site_similarity_decisions("
            "user_id, run_id, group_id, keep_site_id, updated_at"
            ") VALUES ('user', 'run', ?, ?, ?)",
            (
                ("group-multi", "site-a", timestamp),
                ("group-single", "site-d", timestamp),
                # 0017 had no same-group FK on keep_site_id. The 0018 upgrade
                # must not fail or preserve this cross-group dirty value.
                ("group-none", "site-a", timestamp),
            ),
        )
        connection.commit()


def test_site_similarity_snapshot_migration_round_trips(tmp_path: Path) -> None:
    database_path = tmp_path / "site-similarity-0018.sqlite3"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    config = create_alembic_config(database_url)
    command.upgrade(config, "20260731_0016")
    assert SNAPSHOT_TABLES.isdisjoint(_tables(database_path))

    command.upgrade(config, "20260731_0017")
    assert SNAPSHOT_TABLES.issubset(_tables(database_path))
    assert DECISION_MEMBER_TABLE not in _tables(database_path)
    _seed_legacy_decisions(database_path)

    command.upgrade(config, "head")
    command.check(config)
    assert SNAPSHOT_TABLES.issubset(_tables(database_path))
    assert DECISION_MEMBER_TABLE in _tables(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        indexes = {
            str(row[1]): bool(row[2])
            for row in connection.execute("PRAGMA index_list(site_similarity_scan_runs)")
        }
        member_foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(site_similarity_decision_members)"
        ).fetchall()
        selected = connection.execute(
            "SELECT group_id, site_id FROM site_similarity_decision_members "
            "ORDER BY group_id, site_id"
        ).fetchall()
        invalid_choice = connection.execute(
            "SELECT keep_site_id FROM site_similarity_decisions WHERE group_id = 'group-none'"
        ).fetchone()
        connection.execute(
            "INSERT INTO site_similarity_decision_members(user_id, run_id, group_id, site_id) "
            "VALUES ('user', 'run', 'group-multi', 'site-b')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO site_similarity_decision_members("
                "user_id, run_id, group_id, site_id"
                ") VALUES ('user', 'run', 'group-multi', 'site-d')"
            )
        connection.rollback()
        connection.execute(
            "INSERT INTO site_similarity_decision_members(user_id, run_id, group_id, site_id) "
            "VALUES ('user', 'run', 'group-multi', 'site-b')"
        )
        connection.commit()
    assert indexes["uq_site_similarity_ready_run_per_user"] is True
    assert len({row[0] for row in member_foreign_keys}) == 2
    assert selected == [("group-multi", "site-a"), ("group-single", "site-d")]
    assert invalid_choice == (None,)

    command.downgrade(config, "20260731_0017")
    assert DECISION_MEMBER_TABLE not in _tables(database_path)
    with sqlite3.connect(database_path) as connection:
        downgraded = connection.execute(
            "SELECT group_id, keep_site_id FROM site_similarity_decisions ORDER BY group_id"
        ).fetchall()
    assert downgraded == [
        ("group-multi", None),
        ("group-none", None),
        ("group-single", "site-d"),
    ]

    command.downgrade(config, "20260731_0016")
    assert SNAPSHOT_TABLES.isdisjoint(_tables(database_path))
    assert DECISION_MEMBER_TABLE not in _tables(database_path)
    command.upgrade(config, "head")
    command.check(config)
    assert SNAPSHOT_TABLES.issubset(_tables(database_path))
    assert DECISION_MEMBER_TABLE in _tables(database_path)
