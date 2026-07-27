from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from webhub.config import Settings
from webhub.db.migrations import upgrade_database
from webhub.main import create_app

ORIGIN = {"Origin": "http://testserver"}
_REAL_ASYNC_CLIENT = httpx.AsyncClient


@pytest.fixture
def indexed_client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'main.sqlite3').as_posix()}",
        data_directory=tmp_path,
        provider_master_key=b"provider-test-master-key-32bytes",
    )
    upgrade_database(settings.database_url)
    with TestClient(create_app(settings=settings)) as client:
        assert (
            client.post(
                "/api/auth/register",
                json={"username": "alice", "password": "a sufficiently secure password"},
                headers=ORIGIN,
            ).status_code
            == 201
        )
        for index in range(3):
            created = client.post(
                "/api/library/sites",
                json={"name": f"站点 {index}", "url": f"https://s{index}.example.com"},
                headers=ORIGIN,
            )
            assert created.status_code == 201, created.text
        yield client


def _no_vendor_calls(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": []})

    def factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(record)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return seen


def test_reading_the_status_never_spends_anything(
    indexed_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A status page that quietly started indexing would bill the user for looking."""

    seen = _no_vendor_calls(monkeypatch)
    response = indexed_client.get("/api/search/index")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["configured"] is False
    assert body["total_sites"] == 3
    assert body["pending"] == 0
    assert body["estimated_requests"] == 0
    assert body["running"] is False
    assert seen == []


def test_rebuilding_without_a_provider_reports_nothing_scheduled(
    indexed_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No embedding Provider is a missing capability, not a user error."""

    seen = _no_vendor_calls(monkeypatch)
    response = indexed_client.post(
        "/api/search/index/rebuild",
        json={"drop_existing": False, "limit": 512},
        headers=ORIGIN,
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"scheduled": False, "dropped": 0, "estimated_requests": 0}
    assert seen == []


def test_the_index_endpoints_require_a_session(tmp_path: Path) -> None:
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'anon.sqlite3').as_posix()}",
        data_directory=tmp_path,
        provider_master_key=b"provider-test-master-key-32bytes",
    )
    upgrade_database(settings.database_url)
    with TestClient(create_app(settings=settings)) as client:
        assert client.get("/api/search/index").status_code == 401
        assert (
            client.post(
                "/api/search/index/rebuild",
                json={"drop_existing": False, "limit": 512},
                headers=ORIGIN,
            ).status_code
            == 401
        )


def test_rebuild_rejects_a_cross_site_origin(indexed_client: TestClient) -> None:
    """Spending money must not be triggerable from another site."""

    response = indexed_client.post(
        "/api/search/index/rebuild",
        json={"drop_existing": False, "limit": 512},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403, response.text


def test_an_unknown_field_is_refused(indexed_client: TestClient) -> None:
    response = indexed_client.post(
        "/api/search/index/rebuild",
        json={"drop_existing": False, "limit": 512, "force": True},
        headers=ORIGIN,
    )
    assert response.status_code == 422


def test_rebuilding_while_a_pass_runs_does_not_drop_the_index(
    indexed_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure this prevents: one mis-click wiping every vector for nothing.

    Dropping before checking whether a pass can be scheduled means an account
    that already has one running loses its whole index and gets no replacement.
    """

    from webhub.search import routes as search_routes

    dropped_calls: list[str] = []

    async def spy_drop(_session: object, user_id: str) -> int:
        dropped_calls.append(user_id)
        return 99

    monkeypatch.setattr(search_routes, "drop_index", spy_drop)
    monkeypatch.setattr(search_routes.worker, "is_running", lambda _user_id: True)

    response = indexed_client.post(
        "/api/search/index/rebuild",
        json={"drop_existing": True, "limit": 512},
        headers=ORIGIN,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scheduled"] is False
    assert body["dropped"] == 0
    assert dropped_calls == [], "已有一轮在跑时绝不能先丢索引"
