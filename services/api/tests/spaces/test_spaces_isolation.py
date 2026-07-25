import asyncio
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from webhub.config import Settings
from webhub.db.database import Database
from webhub.db.migrations import upgrade_database
from webhub.main import create_app
from webhub.spaces import service as spaces_service
from webhub.spaces.schemas import SpaceReorderRequest

COOKIE_NAME = "webhub_session"
ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture
def isolated_spaces(tmp_path: Path) -> Iterator[tuple[TestClient, Path]]:
    database_path = tmp_path / "main.sqlite3"
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        data_directory=tmp_path,
    )
    upgrade_database(settings.database_url)
    with TestClient(create_app(settings=settings)) as client:
        yield client, database_path


def _register(client: TestClient, username: str) -> tuple[str, str]:
    response = client.post(
        "/api/auth/register",
        json={"username": username, "password": "a sufficiently secure password"},
        headers=ORIGIN,
    )
    assert response.status_code == 201, response.text
    token = response.cookies.get(COOKIE_NAME)
    assert token
    return str(response.json()["user"]["id"]), token


def _use_token(client: TestClient, token: str) -> None:
    client.cookies.clear()
    client.cookies.set(COOKIE_NAME, token)


def _space(client: TestClient, name: str) -> dict[str, object]:
    response = client.post("/api/spaces", json={"name": name}, headers=ORIGIN)
    assert response.status_code == 201, response.text
    return response.json()


def _site(client: TestClient, name: str) -> dict[str, object]:
    response = client.post(
        "/api/library/sites",
        json={"name": name, "url": f"https://{name.casefold()}.example.com"},
        headers=ORIGIN,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _add_member(
    client: TestClient, space_id: object, site_id: object, expected_version: int
) -> dict[str, object]:
    response = client.post(
        f"/api/spaces/{space_id}/members",
        json={"expected_version": expected_version, "site_id": site_id},
        headers=ORIGIN,
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_cross_account_space_ids_and_relationship_tampering_are_hidden(
    isolated_spaces: tuple[TestClient, Path],
) -> None:
    client, database_path = isolated_spaces
    alice_id, alice_token = _register(client, "alice")
    alice_site = _site(client, "Alice")
    alice_space = _space(client, "Alice private")
    alice_second_space = _space(client, "Alice second")
    _add_member(client, alice_space["id"], alice_site["id"], 1)
    alice_space_cursor = client.get(
        "/api/spaces", params={"sort": "name", "direction": "asc", "limit": 1}
    ).json()["next_cursor"]
    assert alice_space_cursor

    client.cookies.clear()
    bob_id, bob_token = _register(client, "bob")
    bob_site = _site(client, "Bob")
    bob_space = _space(client, "Bob private")

    hidden_requests = (
        client.get(f"/api/spaces/{alice_space['id']}"),
        client.patch(
            f"/api/spaces/{alice_space['id']}",
            json={"expected_version": 2, "name": "Stolen"},
            headers=ORIGIN,
        ),
        client.get(f"/api/spaces/{alice_space['id']}/delete-preview"),
        client.delete(
            f"/api/spaces/{alice_space['id']}",
            params={"expected_version": 2},
            headers=ORIGIN,
        ),
        client.post(
            f"/api/spaces/{alice_space['id']}/members",
            json={"expected_version": 2, "site_id": bob_site["id"]},
            headers=ORIGIN,
        ),
        client.patch(
            f"/api/spaces/{alice_space['id']}/members/order",
            json={"expected_version": 2, "ordered_site_ids": [alice_site["id"]]},
            headers=ORIGIN,
        ),
        client.delete(
            f"/api/spaces/{alice_space['id']}/members/{alice_site['id']}",
            params={"expected_version": 2},
            headers=ORIGIN,
        ),
    )
    assert all(response.status_code == 404 for response in hidden_requests)

    cross_account_site = client.post(
        f"/api/spaces/{bob_space['id']}/members",
        json={"expected_version": 1, "site_id": alice_site["id"]},
        headers=ORIGIN,
    )
    assert cross_account_site.status_code == 404
    assert (
        client.get("/api/library/sites", params={"space_id": alice_space["id"]}).status_code == 404
    )
    cross_account_cursor = client.get(
        "/api/spaces",
        params={
            "sort": "name",
            "direction": "asc",
            "limit": 1,
            "cursor": alice_space_cursor,
        },
    )
    assert cross_account_cursor.status_code == 422

    _use_token(client, alice_token)
    bob_site_in_alice_space = client.post(
        f"/api/spaces/{alice_second_space['id']}/members",
        json={"expected_version": 1, "site_id": bob_site["id"]},
        headers=ORIGIN,
    )
    assert bob_site_in_alice_space.status_code == 404

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO space_members"
                "(user_id, space_id, site_id, position, created_at) "
                "VALUES (?, ?, ?, 0, CURRENT_TIMESTAMP)",
                (alice_id, alice_second_space["id"], bob_site["id"]),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO space_members"
                "(user_id, space_id, site_id, position, created_at) "
                "VALUES (?, ?, ?, 0, CURRENT_TIMESTAMP)",
                (bob_id, alice_second_space["id"], bob_site["id"]),
            )

    assert client.get(f"/api/spaces/{alice_space['id']}").status_code == 200
    _use_token(client, bob_token)
    assert client.get(f"/api/spaces/{bob_space['id']}").status_code == 200
    assert alice_id != bob_id


def test_reorder_version_claim_is_atomic_across_concurrent_sessions(
    isolated_spaces: tuple[TestClient, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, database_path = isolated_spaces
    user_id, _ = _register(client, "alice")
    alpha = _site(client, "Alpha")
    bravo = _site(client, "Bravo")
    charlie = _site(client, "Charlie")
    space = _space(client, "Concurrent")
    _add_member(client, space["id"], alpha["id"], 1)
    _add_member(client, space["id"], bravo["id"], 2)
    _add_member(client, space["id"], charlie["id"], 3)
    database = Database(f"sqlite+aiosqlite:///{database_path.as_posix()}")

    async def race_reorders() -> list[tuple[str, int | None]]:
        original_owned_space = spaces_service._owned_space
        ready_count = 0
        ready_lock = asyncio.Lock()
        release = asyncio.Event()

        async def synchronized_owned_space(session, owner_id: str, space_id: str):
            nonlocal ready_count
            owned_space = await original_owned_space(session, owner_id, space_id)
            async with ready_lock:
                ready_count += 1
                if ready_count == 2:
                    release.set()
            await asyncio.wait_for(release.wait(), timeout=5)
            return owned_space

        monkeypatch.setattr(spaces_service, "_owned_space", synchronized_owned_space)

        async def move(site_id: str) -> tuple[str, int | None]:
            async with database.sessions() as session:
                try:
                    result = await spaces_service.reorder_members(
                        session,
                        user_id,
                        str(space["id"]),
                        SpaceReorderRequest(
                            expected_version=4,
                            ordered_site_ids=[site_id],
                            before_site_id=str(alpha["id"]),
                        ),
                    )
                except spaces_service.SpaceConflictError:
                    return "conflict", None
                return "updated", result.version

        try:
            return list(await asyncio.gather(move(str(bravo["id"])), move(str(charlie["id"]))))
        finally:
            await database.dispose()

    outcomes = asyncio.run(race_reorders())
    assert sorted(outcome[0] for outcome in outcomes) == ["conflict", "updated"]
    assert next(outcome[1] for outcome in outcomes if outcome[0] == "updated") == 5

    stored = client.get(f"/api/spaces/{space['id']}")
    assert stored.status_code == 200
    assert stored.json()["version"] == 5
    stored_names = [member["site"]["name"] for member in stored.json()["members"]]
    assert stored_names in (["Bravo", "Alpha", "Charlie"], ["Charlie", "Alpha", "Bravo"])
