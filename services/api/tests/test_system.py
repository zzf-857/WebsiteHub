from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from webhub.config import Settings
from webhub.db.migrations import upgrade_database
from webhub.main import create_app


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    database_path = tmp_path / "system.sqlite3"
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        data_directory=tmp_path,
    )
    upgrade_database(settings.database_url)
    with TestClient(create_app(settings=settings)) as test_client:
        yield test_client


def test_health_reports_service_identity(client: TestClient) -> None:
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "webhub-api",
        "version": "0.1.0",
        "environment": "test",
    }


def test_readiness_reports_api_and_database_checks(client: TestClient) -> None:
    response = client.get("/api/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"api": "ok", "database": "ok"},
    }
