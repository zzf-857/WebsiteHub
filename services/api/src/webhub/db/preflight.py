from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

from sqlalchemy.engine import make_url

from webhub.db import models as _models  # noqa: F401
from webhub.db.base import Base

ALEMBIC_VERSION_TABLE = "alembic_version"
WEBHUB_MANAGED_TABLES = frozenset(Base.metadata.tables) | {"site_search"}


class MigrationPreflightError(RuntimeError):
    pass


def assert_upgrade_safe(database_url: str) -> None:
    """Reject an unversioned SQLite schema before Alembic can run non-transactional DDL."""
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        return

    database_path = Path(url.database).expanduser().resolve()
    if not database_path.is_file():
        return

    with closing(_open_sqlite_read_only(database_path)) as connection:
        table_names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        versions = _read_alembic_versions(connection, table_names)

    if versions:
        return

    managed_tables = sorted(table_names & WEBHUB_MANAGED_TABLES)
    if not managed_tables:
        return

    database_files = ", ".join(
        f"`{database_path.name}{suffix}`" for suffix in ("", "-wal", "-shm")
    )
    table_summary = ", ".join(managed_tables[:5])
    if len(managed_tables) > 5:
        table_summary = f"{table_summary}, ..."
    raise MigrationPreflightError(
        "Refusing to upgrade an unversioned SQLite database that already contains "
        f"WebHub tables ({table_summary}). Stop all WebHub website and API processes, "
        f"archive {database_files} together, then create a fresh database with "
        "`webhub-db upgrade`. Automatic stamp/adopt recovery is intentionally disabled."
    )


def _open_sqlite_read_only(database_path: Path) -> sqlite3.Connection:
    encoded_path = quote(database_path.as_posix(), safe="/:")
    connection = sqlite3.connect(f"file:{encoded_path}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only = ON")
    return connection


def _read_alembic_versions(
    connection: sqlite3.Connection,
    table_names: set[str],
) -> set[str]:
    if ALEMBIC_VERSION_TABLE not in table_names:
        return set()
    return {
        str(row[0])
        for row in connection.execute(
            f'SELECT version_num FROM "{ALEMBIC_VERSION_TABLE}"'
        )
        if row[0]
    }
