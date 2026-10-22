"""Q46 收藏库管理模式与详情页的后端补充：Space 筛选下的全选快照、使用记录字段。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from webhub.config import Settings
from webhub.db.migrations import upgrade_database
from webhub.ingestion.worker import AnalysisSchedule
from webhub.main import create_app

ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture(autouse=True)
def _no_background_analysis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "webhub.ingestion.worker.schedule_analysis",
        lambda *_args, **_kwargs: AnalysisSchedule(queued=0, already_queued=0, rejected=0),
    )
    monkeypatch.setattr(
        "webhub.ingestion.worker.ensure_auto_backfill",
        lambda *_args, **_kwargs: False,
    )


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'main.sqlite3').as_posix()}"
    settings = Settings(environment="test", database_url=database_url, data_directory=tmp_path)
    upgrade_database(settings.database_url)
    with TestClient(create_app(settings=settings)) as test_client:
        yield test_client


def _register(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/register",
        json={"username": username, "password": "a sufficiently secure password"},
        headers=ORIGIN,
    )
    assert response.status_code == 201, response.text


def _site(client: TestClient, url: str) -> dict[str, object]:
    response = client.post(
        "/api/library/sites",
        json={"name": url[:80], "url": url},
        headers=ORIGIN,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _space_with(client: TestClient, name: str, *site_ids: object) -> dict[str, object]:
    space = client.post("/api/spaces", json={"name": name}, headers=ORIGIN)
    assert space.status_code == 201, space.text
    state = space.json()
    version = state["version"]
    for site_id in site_ids:
        added = client.post(
            f"/api/spaces/{state['id']}/members",
            json={"expected_version": version, "site_id": site_id},
            headers=ORIGIN,
        )
        assert added.status_code == 201, added.text
        version = added.json()["space"]["version"]
    return state


def test_selection_snapshot_honours_the_space_filter_like_the_list(client: TestClient) -> None:
    _register(client, "alice")
    first = _site(client, "https://github.com/acme/first")
    second = _site(client, "https://example.com/blog/second")
    _site(client, "https://example.org/")
    space = _space_with(client, "Reading", first["id"], second["id"])

    listed = client.get("/api/library/sites", params={"space_id": space["id"]})
    assert listed.status_code == 200, listed.text
    listed_ids = {item["id"] for item in listed.json()["items"]}
    assert listed_ids == {first["id"], second["id"]}

    selection = client.get("/api/library/sites/selection", params={"space_id": space["id"]})
    assert selection.status_code == 200, selection.text
    assert {item["id"] for item in selection.json()["items"]} == listed_ids

    # 与其他筛选取交集：Space 内的仓库只有一条
    narrowed = client.get(
        "/api/library/sites/selection",
        params={"space_id": space["id"], "kind": "repo"},
    )
    assert [item["id"] for item in narrowed.json()["items"]] == [first["id"]]

    everything = client.get("/api/library/sites/selection")
    assert len(everything.json()["items"]) == 3


def test_selection_space_filter_is_account_scoped(client: TestClient) -> None:
    _register(client, "alice")
    site = _site(client, "https://example.com/")
    space = _space_with(client, "Private", site["id"])

    client.cookies.clear()
    _register(client, "mallory")
    response = client.get("/api/library/sites/selection", params={"space_id": space["id"]})
    assert response.status_code == 404


def test_site_response_reports_snooze_count_and_due_time(client: TestClient) -> None:
    _register(client, "alice")
    site = _site(client, "https://example.com/blog/post")
    assert site["resurface_count"] == 0
    assert site["resurface_due_at"] is None

    before = datetime.now(UTC)
    snoozed = client.put(
        f"/api/library/sites/{site['id']}/state",
        json={"snooze_days": 7},
        headers=ORIGIN,
    )
    assert snoozed.status_code == 200, snoozed.text
    payload = snoozed.json()
    assert payload["resurface_count"] == 1
    due = datetime.fromisoformat(payload["resurface_due_at"])
    due = due if due.tzinfo else due.replace(tzinfo=UTC)
    assert before + timedelta(days=7) - timedelta(seconds=5) <= due
    assert due <= datetime.now(UTC) + timedelta(days=7, seconds=5)

    # 撤销推迟清掉到期时间，但推迟过的次数如实保留
    cleared = client.put(
        f"/api/library/sites/{site['id']}/state",
        json={"snooze_days": 0},
        headers=ORIGIN,
    ).json()
    assert (cleared["resurface_count"], cleared["resurface_due_at"]) == (1, None)

    detail = client.get(f"/api/library/sites/{site['id']}").json()
    assert detail["resurface_count"] == 1
    listed = client.get("/api/library/sites").json()["items"][0]
    assert listed["resurface_count"] == 1
    assert listed["resurface_due_at"] is None
