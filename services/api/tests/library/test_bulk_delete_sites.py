from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from webhub.config import Settings
from webhub.db.migrations import upgrade_database
from webhub.main import create_app

COOKIE_NAME = "webhub_session"
ORIGIN = {"Origin": "http://testserver"}
BULK_DELETE_PATH = "/api/library/sites/bulk-delete"


@pytest.fixture
def bulk_delete_client(tmp_path: Path) -> Iterator[TestClient]:
    database_path = tmp_path / "main.sqlite3"
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        data_directory=tmp_path,
    )
    upgrade_database(settings.database_url)
    with TestClient(create_app(settings=settings)) as client:
        yield client


def _register(client: TestClient, username: str) -> str:
    response = client.post(
        "/api/auth/register",
        json={"username": username, "password": "a sufficiently secure password"},
        headers=ORIGIN,
    )
    assert response.status_code == 201, response.text
    token = response.cookies.get(COOKIE_NAME)
    assert token
    return token


def _use_token(client: TestClient, token: str) -> None:
    client.cookies.clear()
    client.cookies.set(COOKIE_NAME, token)


def _site(client: TestClient, name: str) -> dict[str, object]:
    response = client.post(
        "/api/library/sites",
        json={"name": name, "url": f"https://{name.casefold()}.example.com"},
        headers=ORIGIN,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _bulk_items(*sites: dict[str, object]) -> list[dict[str, object]]:
    return [
        {"site_id": site["id"], "expected_version": site["version"]}
        for site in sites
    ]


def test_bulk_delete_is_atomic_and_updates_each_related_space_once(
    bulk_delete_client: TestClient,
) -> None:
    client = bulk_delete_client
    _register(client, "alice")
    first = _site(client, "First")
    second = _site(client, "Second")
    primary = client.post("/api/spaces", json={"name": "Primary"}, headers=ORIGIN).json()
    secondary = client.post("/api/spaces", json={"name": "Secondary"}, headers=ORIGIN).json()

    primary_state = client.post(
        f"/api/spaces/{primary['id']}/members",
        json={"expected_version": primary["version"], "site_id": first["id"]},
        headers=ORIGIN,
    ).json()
    primary_state = client.post(
        f"/api/spaces/{primary['id']}/members",
        json={
            "expected_version": primary_state["space"]["version"],
            "site_id": second["id"],
        },
        headers=ORIGIN,
    ).json()
    secondary_state = client.post(
        f"/api/spaces/{secondary['id']}/members",
        json={"expected_version": secondary["version"], "site_id": first["id"]},
        headers=ORIGIN,
    ).json()

    deleted = client.post(
        BULK_DELETE_PATH,
        json={"items": _bulk_items(first, second)},
        headers=ORIGIN,
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {
        "message": "已删除 2 个网站",
        "deleted_site_ids": [first["id"], second["id"]],
    }
    assert client.get(f"/api/library/sites/{first['id']}").status_code == 404
    assert client.get(f"/api/library/sites/{second['id']}").status_code == 404

    primary_after = client.get(f"/api/spaces/{primary['id']}").json()
    secondary_after = client.get(f"/api/spaces/{secondary['id']}").json()
    assert primary_after["version"] == primary_state["space"]["version"] + 1
    assert secondary_after["version"] == secondary_state["space"]["version"] + 1
    assert primary_after["member_count"] == secondary_after["member_count"] == 0


def test_one_stale_version_rejects_the_whole_batch(bulk_delete_client: TestClient) -> None:
    client = bulk_delete_client
    _register(client, "alice")
    first = _site(client, "First")
    second = _site(client, "Second")
    updated_second = client.patch(
        f"/api/library/sites/{second['id']}",
        json={"expected_version": second["version"], "pinned": True},
        headers=ORIGIN,
    ).json()

    rejected = client.post(
        BULK_DELETE_PATH,
        json={"items": _bulk_items(first, second)},
        headers=ORIGIN,
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"] == {
        "code": "bulk_delete_conflict",
        "message": "所选网站已发生变化，请刷新后重新选择",
    }
    assert client.get(f"/api/library/sites/{first['id']}").status_code == 200
    assert (
        client.get(f"/api/library/sites/{second['id']}").json()["version"]
        == updated_second["version"]
    )


def test_foreign_site_id_uses_the_same_conflict_and_deletes_nothing(
    bulk_delete_client: TestClient,
) -> None:
    client = bulk_delete_client
    alice_token = _register(client, "alice")
    alice_site = _site(client, "Alice")
    client.cookies.clear()
    _register(client, "bob")
    bob_site = _site(client, "Bob")

    rejected = client.post(
        BULK_DELETE_PATH,
        json={"items": _bulk_items(bob_site, alice_site)},
        headers=ORIGIN,
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"] == {
        "code": "bulk_delete_conflict",
        "message": "所选网站已发生变化，请刷新后重新选择",
    }
    assert client.get(f"/api/library/sites/{bob_site['id']}").status_code == 200
    _use_token(client, alice_token)
    assert client.get(f"/api/library/sites/{alice_site['id']}").status_code == 200


def test_bulk_delete_validates_bounds_duplicates_auth_and_origin(
    bulk_delete_client: TestClient,
) -> None:
    client = bulk_delete_client
    example = {"site_id": "site-1", "expected_version": 1}
    assert (
        client.post(BULK_DELETE_PATH, json={"items": [example]}, headers=ORIGIN).status_code
        == 401
    )

    _register(client, "alice")
    assert client.post(BULK_DELETE_PATH, json={"items": []}, headers=ORIGIN).status_code == 422
    assert (
        client.post(
            BULK_DELETE_PATH,
            json={"items": [example, example]},
            headers=ORIGIN,
        ).status_code
        == 422
    )
    too_many = [
        {"site_id": f"site-{index}", "expected_version": 1}
        for index in range(101)
    ]
    assert (
        client.post(
            BULK_DELETE_PATH,
            json={"items": too_many},
            headers=ORIGIN,
        ).status_code
        == 422
    )
    assert client.post(BULK_DELETE_PATH, json={"items": [example]}).status_code == 403
    assert (
        client.post(
            BULK_DELETE_PATH,
            json={"items": [example]},
            headers={"Origin": "http://attacker.invalid"},
        ).status_code
        == 403
    )
