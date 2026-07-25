from pathlib import Path

import pytest
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient

from alembic import command
from webhub.config import Settings
from webhub.db.database import DATABASE_SCHEMA_HEADS, DatabaseSchemaError
from webhub.db.migrations import create_alembic_config
from webhub.main import create_app

SAME_ORIGIN_HEADERS = {"Origin": "http://testserver"}


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
