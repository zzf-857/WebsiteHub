import sqlite3
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient

from alembic import command
from webhub.config import Settings
from webhub.db import cli as db_cli
from webhub.db.database import DATABASE_SCHEMA_HEADS, DatabaseSchemaError
from webhub.db.migrations import create_alembic_config, upgrade_database
from webhub.main import create_app

SAME_ORIGIN_HEADERS = {"Origin": "http://testserver"}
BOOKMARK_TABLES = {
    "bookmark_import_checkpoints",
    "bookmark_import_current_runs",
    "bookmark_import_jobs",
    "bookmark_import_runs",
    "bookmark_import_snapshots",
    "bookmark_source_folders",
    "bookmark_source_occurrences",
    "bookmark_staging_candidate_folders",
    "bookmark_staging_candidate_occurrences",
    "bookmark_staging_candidate_site_matches",
    "bookmark_staging_candidates",
    "bookmark_staging_folders",
    "bookmark_staging_occurrences",
    "site_import_origins",
}
_TERMINAL_PARSE_FACT_TABLES = (
    "bookmark_staging_folders",
    "bookmark_staging_occurrences",
    "bookmark_staging_candidate_occurrences",
    "bookmark_staging_candidate_folders",
)
BOOKMARK_TRIGGERS = {
    "bookmark_import_runs_terminal_immutable",
    "bookmark_import_current_run_insert_complete",
    "bookmark_import_current_run_update_complete",
    "bookmark_import_checkpoints_terminal_parse_insert",
    "bookmark_import_checkpoints_terminal_parse_update",
    "bookmark_import_checkpoints_terminal_parse_delete",
    "bookmark_staging_candidates_terminal_insert",
    "bookmark_staging_candidates_terminal_update",
    "bookmark_staging_candidates_terminal_delete",
} | {
    f"{table_name}_terminal_{operation}"
    for table_name in _TERMINAL_PARSE_FACT_TABLES
    for operation in ("insert", "update", "delete")
}


def test_db_cli_allows_an_existing_empty_sqlite_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "empty.sqlite3"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    sqlite3.connect(database_path).close()
    monkeypatch.setattr(
        db_cli,
        "get_settings",
        lambda: SimpleNamespace(database_url=database_url),
    )

    assert db_cli.main(["upgrade"]) == 0

    config = create_alembic_config(database_url)
    expected_heads = set(ScriptDirectory.from_config(config).get_heads())
    with sqlite3.connect(database_path) as connection:
        actual_versions = {
            str(row[0]) for row in connection.execute("SELECT version_num FROM alembic_version")
        }
    assert actual_versions == expected_heads


def test_db_cli_refuses_a_half_migrated_unversioned_database_before_ddl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "main.sqlite3"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL);
            CREATE TABLE users (id VARCHAR(36) NOT NULL PRIMARY KEY);
            """
        )
        schema_before = connection.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()

    monkeypatch.setattr(
        db_cli,
        "get_settings",
        lambda: SimpleNamespace(database_url=database_url),
    )

    with pytest.raises(SystemExit) as raised:
        db_cli.main(["upgrade"])

    assert raised.value.code == 2
    error_output = capsys.readouterr().err
    assert "Stop all WebHub website and API processes" in error_output
    assert "`main.sqlite3`, `main.sqlite3-wal`, `main.sqlite3-shm`" in error_output
    assert "fresh database" in error_output
    assert "stamp/adopt" in error_output
    with sqlite3.connect(database_path) as connection:
        schema_after = connection.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        versions = connection.execute("SELECT version_num FROM alembic_version").fetchall()
    assert schema_after == schema_before
    assert versions == []


def test_upgrade_preflight_allows_a_versioned_database(tmp_path: Path) -> None:
    database_path = tmp_path / "versioned.sqlite3"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    config = create_alembic_config(database_url)
    command.upgrade(config, "head")

    upgrade_database(database_url)

    expected_heads = set(ScriptDirectory.from_config(config).get_heads())
    with sqlite3.connect(database_path) as connection:
        actual_versions = {
            str(row[0]) for row in connection.execute("SELECT version_num FROM alembic_version")
        }
    assert actual_versions == expected_heads


@pytest.fixture
def bookmark_candidate_trigger_database(
    tmp_path: Path,
) -> Iterator[tuple[sqlite3.Connection, dict[str, str]]]:
    database_path = tmp_path / "candidate-triggers.sqlite3"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    command.upgrade(create_alembic_config(database_url), "head")

    ids = {
        "user": "user",
        "running_run": "run-running",
        "finalizing_run": "run-finalizing",
        "complete_run": "run-complete",
        "running_candidate": "candidate-running",
        "finalizing_candidate": "candidate-finalizing",
        "complete_candidate": "candidate-complete",
        "finalizing_folder": "folder-finalizing",
    }
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        """
        INSERT INTO users(
            id, username, display_name, password_hash, is_active, created_at, updated_at
        ) VALUES (
            'user', 'trigger-user', 'Trigger User', 'test-hash', 1,
            '2026-07-26 00:00:00+00:00', '2026-07-26 00:00:00+00:00'
        );
        INSERT INTO bookmark_import_snapshots(
            id, user_id, source_sha256, source_size_bytes, source_format, original_filename,
            storage_key, detected_encoding, request_idempotency_key_hash, created_at
        ) VALUES (
            'snapshot', 'user', printf('%064d', 1), 1, 'netscape_html', 'bookmarks.html',
            'imports/candidate-trigger-test.html', 'utf-8', printf('%064d', 2),
            '2026-07-26 00:00:00+00:00'
        );
        INSERT INTO bookmark_import_jobs(
            id, user_id, snapshot_id, state, parser_version, normalizer_version, skill_version,
            version, preview_version, progress_completed, progress_total, classification_budget,
            classification_used, created_at, updated_at
        ) VALUES (
            'job', 'user', 'snapshot', 'parsing', 'parser-v2', 'normalizer-v2', 'skill-v2',
            1, 0, 0, 0, 0, 0,
            '2026-07-26 00:00:00+00:00', '2026-07-26 00:00:00+00:00'
        );
        INSERT INTO bookmark_import_runs(
            id, user_id, job_id, attempt_number, state, run_idempotency_key_hash, input_hash,
            parser_version, normalizer_version, source_sequence_count, folder_count,
            occurrence_count, candidate_count, created_at
        ) VALUES
            ('run-running', 'user', 'job', 1, 'running', printf('%064d', 3),
             printf('%064d', 4), 'parser-v2', 'normalizer-v2', 0, 0, 0, 0,
             '2026-07-26 00:00:00+00:00'),
            ('run-finalizing', 'user', 'job', 2, 'running', printf('%064d', 5),
             printf('%064d', 6), 'parser-v2', 'normalizer-v2', 0, 0, 0, 0,
             '2026-07-26 00:00:00+00:00'),
            ('run-complete', 'user', 'job', 3, 'running', printf('%064d', 7),
             printf('%064d', 8), 'parser-v2', 'normalizer-v2', 0, 0, 0, 0,
             '2026-07-26 00:00:00+00:00');
        INSERT INTO bookmark_staging_candidates(
            id, user_id, run_id, identity_url, identity_hash, display_title, host, fetch_policy,
            has_sensitive_url, proposed_action, occurrence_count, first_source_sequence, created_at
        ) VALUES
            ('candidate-running', 'user', 'run-running', 'https://running.example/',
             printf('%064d', 9), 'Running title', 'running.example',
             'public_revalidation_required', 0, 'create', 1, 1,
             '2026-07-26 00:00:00+00:00'),
            ('candidate-finalizing', 'user', 'run-finalizing', 'https://finalizing.example/',
             printf('%064d', 10), 'Finalizing title', 'finalizing.example',
             'public_revalidation_required', 0, 'create', 1, 1,
             '2026-07-26 00:00:00+00:00'),
            ('candidate-complete', 'user', 'run-complete', 'https://complete.example/',
             printf('%064d', 11), 'Complete title', 'complete.example',
             'public_revalidation_required', 0, 'create', 1, 1,
             '2026-07-26 00:00:00+00:00');
        INSERT INTO bookmark_staging_folders(
            id, user_id, run_id, source_folder_key, source_sequence, source_order, depth, title,
            display_path, created_at
        ) VALUES (
            'folder-finalizing', 'user', 'run-finalizing', 'folder-finalizing', 1, 1, 1,
            'Folder', 'Folder', '2026-07-26 00:00:00+00:00'
        );
        INSERT INTO bookmark_staging_candidate_folders(
            user_id, run_id, candidate_id, folder_scope_key, folder_id, occurrence_count,
            first_source_sequence
        ) VALUES (
            'user', 'run-finalizing', 'candidate-finalizing', 'folder-finalizing',
            'folder-finalizing', 1, 1
        );
        UPDATE bookmark_import_runs
        SET state = 'finalizing', completion_hash = printf('%064d', 12)
        WHERE id = 'run-finalizing';
        UPDATE bookmark_import_runs
        SET state = 'complete', completion_hash = printf('%064d', 13),
            completed_at = '2026-07-26 00:00:00+00:00'
        WHERE id = 'run-complete';
        """
    )
    connection.commit()

    try:
        yield connection, ids
    finally:
        connection.close()


def test_migrations_round_trip_without_schema_drift(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "nested" / "migrated.sqlite3"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    alembic_config = create_alembic_config(database_url)
    assert set(ScriptDirectory.from_config(alembic_config).get_heads()) == DATABASE_SCHEMA_HEADS
    command.upgrade(alembic_config, "head")
    command.check(alembic_config)
    command.downgrade(alembic_config, "base")
    command.upgrade(alembic_config, "head")
    command.check(alembic_config)

    settings = Settings(
        environment="test",
        database_url=database_url,
        data_directory=tmp_path,
    )
    with TestClient(create_app(settings=settings)) as client:
        ready = client.get("/api/ready")
        registered = client.post(
            "/api/auth/register",
            json={"username": "migrated", "password": "a secure migration password"},
            headers=SAME_ORIGIN_HEADERS,
        )

    assert ready.status_code == 200
    assert registered.status_code == 201


def test_category_icon_backfill_upgrades_0008_through_52_to_head(tmp_path: Path) -> None:
    database_path = tmp_path / "category-icon-backfill.sqlite3"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    alembic_config = create_alembic_config(database_url)
    command.upgrade(alembic_config, "20260727_0008")

    timestamp = "2026-07-27 00:00:00+00:00"
    categories = (
        ("cat-ai", "人工智能研究"),
        ("cat-database", "数据库"),
        ("cat-tools", "mail tools"),
        ("cat-unknown", "未识别分类"),
        ("cat-explicit", "开发"),
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO users(id, username, display_name, password_hash, is_active, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("user", "icon-user", "Icon User", "test-hash", 1, timestamp, timestamp),
        )
        connection.executemany(
            "INSERT INTO categories(id, user_id, name, normalized_name, is_default, "
            "created_at, updated_at) VALUES (?, 'user', ?, ?, 0, ?, ?)",
            (
                (category_id, name, name.casefold(), timestamp, timestamp)
                for category_id, name in categories
            ),
        )
        connection.commit()
        columns_at_0008 = {
            row[1] for row in connection.execute("PRAGMA table_info(categories)")
        }
    assert "icon" not in columns_at_0008

    command.upgrade(alembic_config, "52c3f6173b38")
    with sqlite3.connect(database_path) as connection:
        icons_at_52 = dict(connection.execute("SELECT id, icon FROM categories"))
        connection.execute("UPDATE categories SET icon = 'Star' WHERE id = 'cat-explicit'")
        connection.commit()
    assert set(icons_at_52.values()) == {"Folder"}

    command.upgrade(alembic_config, "head")
    command.check(alembic_config)
    with sqlite3.connect(database_path) as connection:
        icons_at_head = dict(connection.execute("SELECT id, icon FROM categories"))
        version_at_head = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert icons_at_head == {
        "cat-ai": "Bot",
        "cat-database": "Database",
        "cat-explicit": "Star",
        "cat-tools": "Wrench",
        "cat-unknown": "Folder",
    }
    assert version_at_head is not None
    assert {version_at_head[0]} == DATABASE_SCHEMA_HEADS

    command.downgrade(alembic_config, "52c3f6173b38")
    with sqlite3.connect(database_path) as connection:
        icons_after_downgrade = dict(connection.execute("SELECT id, icon FROM categories"))
    assert icons_after_downgrade == icons_at_head

    command.upgrade(alembic_config, "head")
    command.check(alembic_config)
    with sqlite3.connect(database_path) as connection:
        icons_after_second_upgrade = dict(connection.execute("SELECT id, icon FROM categories"))
    assert icons_after_second_upgrade == icons_at_head


def test_application_rejects_an_unversioned_database(tmp_path: Path) -> None:
    database_path = tmp_path / "unversioned.sqlite3"
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        data_directory=tmp_path,
    )

    with (
        pytest.raises(DatabaseSchemaError, match="webhub-db upgrade"),
        TestClient(create_app(settings=settings)),
    ):
        pass


def test_bookmark_schema_upgrade_preserves_existing_account_data(tmp_path: Path) -> None:
    database_path = tmp_path / "existing-0003.sqlite3"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    alembic_config = create_alembic_config(database_url)
    command.upgrade(alembic_config, "20260726_0003")

    user_id = "00000000-0000-0000-0000-000000000001"
    category_id = "00000000-0000-0000-0000-000000000002"
    tag_id = "00000000-0000-0000-0000-000000000003"
    site_id = "00000000-0000-0000-0000-000000000004"
    space_id = "00000000-0000-0000-0000-000000000005"
    timestamp = "2026-07-26 00:00:00+00:00"

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO users(id, username, display_name, password_hash, is_active, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, "existing", "Existing User", "test-hash", 1, timestamp, timestamp),
        )
        connection.execute(
            "INSERT INTO categories(id, user_id, name, normalized_name, is_default, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (category_id, user_id, "未分类", "未分类", 1, timestamp, timestamp),
        )
        connection.execute(
            "INSERT INTO tags(id, user_id, name, normalized_name, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (tag_id, user_id, "保留标签", "保留标签", timestamp, timestamp),
        )
        connection.execute(
            "INSERT INTO sites(id, user_id, category_id, name, normalized_name, original_url, "
            "identity_url, description, favicon_url, pinned, source, analysis_status, version, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                site_id,
                user_id,
                category_id,
                "Existing Site",
                "existing site",
                "https://example.com/?keep=1#fragment",
                "https://example.com/?keep=1#fragment",
                "must survive migration",
                None,
                1,
                "manual",
                "not_analyzed",
                3,
                timestamp,
                timestamp,
            ),
        )
        connection.execute(
            "INSERT INTO site_tags(user_id, site_id, tag_id, created_at) VALUES (?, ?, ?, ?)",
            (user_id, site_id, tag_id, timestamp),
        )
        connection.execute(
            "INSERT INTO spaces(id, user_id, name, normalized_name, version, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (space_id, user_id, "Existing Space", "existing space", 2, timestamp, timestamp),
        )
        connection.execute(
            "INSERT INTO space_members(user_id, space_id, site_id, position, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, space_id, site_id, 0, timestamp),
        )
        connection.commit()

    def assert_existing_data() -> None:
        with sqlite3.connect(database_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            site = connection.execute(
                "SELECT name, identity_url, description, pinned, version FROM sites WHERE id = ?",
                (site_id,),
            ).fetchone()
            fts = connection.execute(
                "SELECT category_name, tag_names FROM site_search "
                "WHERE user_id = ? AND site_id = ?",
                (user_id, site_id),
            ).fetchone()
            member = connection.execute(
                "SELECT position FROM space_members "
                "WHERE user_id = ? AND space_id = ? AND site_id = ?",
                (user_id, space_id, site_id),
            ).fetchone()
            foreign_key_violations = connection.execute("PRAGMA foreign_key_check").fetchall()

        assert site == (
            "Existing Site",
            "https://example.com/?keep=1#fragment",
            "must survive migration",
            1,
            3,
        )
        assert fts == ("未分类", "保留标签")
        assert member == (0,)
        assert foreign_key_violations == []

    def assert_bookmark_schema(*, present: bool) -> None:
        with sqlite3.connect(database_path) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' "
                    "AND (name LIKE 'bookmark_%' OR name = 'site_import_origins')"
                )
            }
            triggers = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'trigger' AND name LIKE 'bookmark_%'"
                )
            }

        assert tables == (BOOKMARK_TABLES if present else set())
        assert triggers == (BOOKMARK_TRIGGERS if present else set())

    command.upgrade(alembic_config, "head")
    command.check(alembic_config)
    assert_existing_data()
    assert_bookmark_schema(present=True)

    command.downgrade(alembic_config, "20260726_0003")
    assert_existing_data()
    assert_bookmark_schema(present=False)

    command.upgrade(alembic_config, "head")
    command.check(alembic_config)
    assert_existing_data()
    assert_bookmark_schema(present=True)


@pytest.mark.parametrize(
    ("candidate_key", "target_run_key"),
    (
        ("finalizing_candidate", "running_run"),
        ("running_candidate", "finalizing_run"),
    ),
)
def test_candidate_update_checks_both_old_and_new_run_state(
    bookmark_candidate_trigger_database: tuple[sqlite3.Connection, dict[str, str]],
    candidate_key: str,
    target_run_key: str,
) -> None:
    connection, ids = bookmark_candidate_trigger_database

    with pytest.raises(sqlite3.IntegrityError, match="candidate structure is immutable"):
        connection.execute(
            "UPDATE bookmark_staging_candidates SET run_id = ? WHERE id = ?",
            (ids[target_run_key], ids[candidate_key]),
        )
    connection.rollback()


def test_finalizing_candidate_allows_only_aggregate_projection_rebuild(
    bookmark_candidate_trigger_database: tuple[sqlite3.Connection, dict[str, str]],
) -> None:
    connection, ids = bookmark_candidate_trigger_database
    candidate_id = ids["finalizing_candidate"]

    connection.execute(
        "UPDATE bookmark_staging_candidates "
        "SET occurrence_count = 2, first_source_sequence = 2, proposed_action = ? WHERE id = ?",
        ("skip_existing", candidate_id),
    )
    connection.execute(
        "DELETE FROM bookmark_staging_candidate_folders "
        "WHERE user_id = ? AND run_id = ? AND candidate_id = ?",
        (ids["user"], ids["finalizing_run"], candidate_id),
    )
    connection.execute(
        "INSERT INTO bookmark_staging_candidate_folders("
        "user_id, run_id, candidate_id, folder_scope_key, folder_id, occurrence_count, "
        "first_source_sequence"
        ") VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            ids["user"],
            ids["finalizing_run"],
            candidate_id,
            "folder-finalizing",
            ids["finalizing_folder"],
            2,
            2,
        ),
    )
    connection.commit()

    assert connection.execute(
        "SELECT occurrence_count, first_source_sequence, proposed_action "
        "FROM bookmark_staging_candidates WHERE id = ?",
        (candidate_id,),
    ).fetchone() == (2, 2, "skip_existing")
    assert connection.execute(
        "SELECT occurrence_count, first_source_sequence "
        "FROM bookmark_staging_candidate_folders WHERE candidate_id = ?",
        (candidate_id,),
    ).fetchone() == (2, 2)

    frozen_updates: tuple[tuple[str, object], ...] = (
        ("identity_url", "https://changed.example/"),
        ("identity_hash", "8" * 64),
        ("display_title", "Changed while finalizing"),
        ("host", "changed.example"),
        ("fetch_policy", "export_metadata_only"),
        ("has_sensitive_url", 1),
    )
    for column, value in frozen_updates:
        with pytest.raises(sqlite3.IntegrityError, match="candidate structure is immutable"):
            connection.execute(
                f"UPDATE bookmark_staging_candidates SET {column} = ? WHERE id = ?",
                (value, candidate_id),
            )
        connection.rollback()


def test_complete_candidate_allows_only_display_and_action_review(
    bookmark_candidate_trigger_database: tuple[sqlite3.Connection, dict[str, str]],
) -> None:
    connection, ids = bookmark_candidate_trigger_database
    candidate_id = ids["complete_candidate"]

    connection.execute(
        "UPDATE bookmark_staging_candidates "
        "SET display_title = ?, proposed_action = ? WHERE id = ?",
        ("Reviewed title", "needs_review", candidate_id),
    )
    connection.commit()
    assert connection.execute(
        "SELECT display_title, proposed_action FROM bookmark_staging_candidates WHERE id = ?",
        (candidate_id,),
    ).fetchone() == ("Reviewed title", "needs_review")

    frozen_updates: tuple[tuple[str, object], ...] = (
        ("identity_url", "https://changed.example/"),
        ("identity_hash", "9" * 64),
        ("host", "changed.example"),
        ("fetch_policy", "export_metadata_only"),
        ("has_sensitive_url", 1),
        ("occurrence_count", 2),
        ("first_source_sequence", 2),
        ("created_at", "2026-07-27 00:00:00+00:00"),
    )
    for column, value in frozen_updates:
        with pytest.raises(sqlite3.IntegrityError, match="candidate structure is immutable"):
            connection.execute(
                f"UPDATE bookmark_staging_candidates SET {column} = ? WHERE id = ?",
                (value, candidate_id),
            )
        connection.rollback()

    with pytest.raises(sqlite3.IntegrityError, match="candidate structure is immutable"):
        connection.execute(
            "DELETE FROM bookmark_staging_candidates WHERE id = ?",
            (candidate_id,),
        )
    connection.rollback()
