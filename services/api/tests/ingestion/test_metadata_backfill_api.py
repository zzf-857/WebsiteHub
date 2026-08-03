from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from webhub.config import Settings
from webhub.db.migrations import upgrade_database
from webhub.ingestion.worker import AnalysisSchedule
from webhub.main import create_app

ORIGIN = {"Origin": "http://testserver"}


@contextmanager
def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'main.sqlite3').as_posix()}",
        data_directory=tmp_path,
        provider_master_key=b"provider-test-master-key-32bytes",
    )
    upgrade_database(settings.database_url)
    monkeypatch.setattr(
        "webhub.library.routes.ingestion_worker.schedule_analysis",
        lambda *_args, **_kwargs: AnalysisSchedule(
            queued=0,
            already_queued=0,
            rejected=0,
        ),
    )
    monkeypatch.setattr(
        "webhub.library.routes.ingestion_worker.ensure_metadata_backfill",
        lambda *_args, **_kwargs: None,
    )
    with TestClient(create_app(settings=settings)) as client:
        registered = client.post(
            "/api/auth/register",
            json={
                "username": "alice",
                "password": "a sufficiently secure password",
            },
            headers=ORIGIN,
        )
        assert registered.status_code == 201, registered.text
        yield client


def _site(client: TestClient, index: int) -> None:
    created = client.post(
        "/api/library/sites",
        json={"name": f"Site {index}", "url": f"https://example{index}.com/"},
        headers=ORIGIN,
    )
    assert created.status_code == 201, created.text


def test_plan_is_exact_and_metadata_mode_does_not_require_model_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _client(tmp_path, monkeypatch) as client:
        for index in range(3):
            _site(client, index)

        metadata_plan = client.get(
            "/api/library/metadata-backfills/plan",
            params={"mode": "metadata", "limit": 2},
        )
        assert metadata_plan.status_code == 200, metadata_plan.text
        assert metadata_plan.json() == {
            "mode": "metadata",
            "requested_limit": 2,
            "max_limit": 500,
            "eligible_count": 3,
            "selected_count": 2,
            "llm_count": 0,
        }

        full_plan = client.get(
            "/api/library/metadata-backfills/plan",
            params={"mode": "full", "limit": 2},
        )
        assert full_plan.status_code == 200, full_plan.text
        assert full_plan.json()["llm_count"] == 2

        missing_provider = client.post(
            "/api/library/metadata-backfills",
            json={"mode": "full", "limit": 2},
            headers=ORIGIN,
        )
        assert missing_provider.status_code == 422
        assert missing_provider.json()["detail"]["code"] == "model_provider_required"

        started = client.post(
            "/api/library/metadata-backfills",
            json={"mode": "metadata", "limit": 2},
            headers=ORIGIN,
        )
        assert started.status_code == 202, started.text
        payload = started.json()
        assert payload["mode"] == "metadata"
        assert payload["total_count"] == 2
        assert payload["queued_count"] == 2
        assert payload["stop_reason"] is None
        assert payload["provider_retry_at"] is None

        active = client.get("/api/library/metadata-backfills/active")
        assert active.status_code == 200
        assert active.json()["id"] == payload["id"]
        assert active.json()["mode"] == "metadata"


def test_mode_specific_limit_is_rejected_before_provider_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _client(tmp_path, monkeypatch) as client:
        response = client.post(
            "/api/library/metadata-backfills",
            json={"mode": "full", "limit": 101},
            headers=ORIGIN,
        )

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "code": "metadata_backfill_limit_exceeded",
        "message": "full 模式每批最多处理 100 个网站。",
    }


def test_start_without_body_uses_safe_metadata_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _site(client, 1)
        response = client.post("/api/library/metadata-backfills", headers=ORIGIN)

    assert response.status_code == 202, response.text
    assert response.json()["mode"] == "metadata"
    assert response.json()["total_count"] == 1
