from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from alembic import command
from webhub.db.migrations import create_alembic_config


def _seed_stopped_run(database_path: Path) -> None:
    timestamp = "2026-07-28 00:00:00+00:00"
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO users(id, username, display_name, password_hash, is_active, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("user", "migration-user", "Migration User", "test-hash", 1, timestamp, timestamp),
        )
        connection.execute(
            "INSERT INTO site_metadata_backfill_runs("
            "id, user_id, state, total_count, queued_count, running_count, complete_count, "
            "limited_count, failed_count, skipped_count, version, lease_token_hash, "
            "lease_expires_at, heartbeat_at, stop_requested, consecutive_provider_failures, "
            "provider_retry_at, created_at, updated_at, completed_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "run",
                "user",
                "completed_with_errors",
                4,
                0,
                0,
                0,
                1,
                3,
                0,
                1,
                None,
                None,
                timestamp,
                1,
                3,
                timestamp,
                timestamp,
                timestamp,
                timestamp,
            ),
        )
        items = (
            ("untouched-a", "site-a", "failed", 0),
            ("untouched-b", "site-b", "failed", 0),
            ("attempted", "site-c", "failed", 1),
            ("limited", "site-d", "limited", 1),
        )
        connection.executemany(
            "INSERT INTO site_metadata_backfill_items("
            "id, user_id, run_id, site_id, expected_version, initial_analysis_status, "
            "requires_llm, origin_key, state, attempt_count, analysis_claimed_at, "
            "lease_token_hash, lease_expires_at, available_at, created_at, updated_at, "
            "completed_at"
            ") VALUES (?, 'user', 'run', ?, 1, 'complete', 1, ?, ?, ?, NULL, NULL, NULL, "
            "NULL, ?, ?, ?)",
            (
                (
                    item_id,
                    site_id,
                    f"https://{site_id}.example",
                    state,
                    attempt_count,
                    timestamp,
                    timestamp,
                    timestamp,
                )
                for item_id, site_id, state, attempt_count in items
            ),
        )
        connection.commit()


def _run_projection(database_path: Path) -> tuple[object, ...]:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT total_count, queued_count, running_count, complete_count, limited_count, "
            "failed_count, skipped_count FROM site_metadata_backfill_runs WHERE id = 'run'"
        ).fetchone()
    assert row is not None
    return row


def _item_states(database_path: Path) -> dict[str, str]:
    with sqlite3.connect(database_path) as connection:
        return dict(
            connection.execute("SELECT id, state FROM site_metadata_backfill_items ORDER BY id")
        )


def test_bounded_backfill_migration_repairs_fuse_counts_and_round_trips(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "metadata-backfill-0016.sqlite3"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    config = create_alembic_config(database_url)
    command.upgrade(config, "20260731_0015")
    _seed_stopped_run(database_path)

    command.upgrade(config, "20260731_0016")
    with sqlite3.connect(database_path) as connection:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(site_metadata_backfill_runs)")
        }
        mode_and_reason = connection.execute(
            "SELECT mode, stop_reason FROM site_metadata_backfill_runs WHERE id = 'run'"
        ).fetchone()
    assert {"mode", "stop_reason"}.issubset(columns)
    assert mode_and_reason == ("full", None)
    assert _run_projection(database_path) == (4, 0, 0, 0, 1, 1, 2)
    assert _item_states(database_path) == {
        "attempted": "failed",
        "limited": "limited",
        "untouched-a": "skipped",
        "untouched-b": "skipped",
    }

    with sqlite3.connect(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE site_metadata_backfill_runs SET mode = 'unbounded' WHERE id = 'run'"
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE site_metadata_backfill_runs SET stop_reason = 'vendor_text' "
                "WHERE id = 'run'"
            )
        connection.rollback()

    command.downgrade(config, "20260731_0015")
    with sqlite3.connect(database_path) as connection:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(site_metadata_backfill_runs)")
        }
    assert "mode" not in columns
    assert "stop_reason" not in columns
    # Downgrading removes the richer columns but must not reintroduce the old
    # false-failure projection. The 0015 schema already accepts skipped items.
    assert _run_projection(database_path) == (4, 0, 0, 0, 1, 1, 2)
    assert _item_states(database_path)["untouched-a"] == "skipped"

    command.upgrade(config, "head")
    command.check(config)
    assert _run_projection(database_path) == (4, 0, 0, 0, 1, 1, 2)
    assert _item_states(database_path)["untouched-a"] == "skipped"
