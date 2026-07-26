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
from webhub.spaces.schemas import (
    SpaceMemberAddRequest,
    SpaceReorderRequest,
    SpaceUpdateRequest,
)

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
    assert all(response.json()["detail"]["code"] == "not_found" for response in hidden_requests)

    cross_account_site = client.post(
        f"/api/spaces/{bob_space['id']}/members",
        json={"expected_version": 1, "site_id": alice_site["id"]},
        headers=ORIGIN,
    )
    assert cross_account_site.status_code == 404
    assert cross_account_site.json()["detail"]["code"] == "not_found"
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
    assert cross_account_cursor.json()["detail"]["code"] == "validation_error"

    _use_token(client, alice_token)
    bob_site_in_alice_space = client.post(
        f"/api/spaces/{alice_second_space['id']}/members",
        json={"expected_version": 1, "site_id": bob_site["id"]},
        headers=ORIGIN,
    )
    assert bob_site_in_alice_space.status_code == 404
    assert bob_site_in_alice_space.json()["detail"]["code"] == "not_found"

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


def test_legacy_invalid_favicon_is_safely_hidden_from_space_members(
    isolated_spaces: tuple[TestClient, Path],
) -> None:
    client, database_path = isolated_spaces
    user_id, _ = _register(client, "alice")
    created_site = client.post(
        "/api/library/sites",
        json={
            "name": "Legacy favicon",
            "url": "https://legacy-space-favicon.example.com",
            "favicon_url": "https://icons.example.com/safe.svg",
        },
        headers=ORIGIN,
    ).json()
    space = _space(client, "Legacy data")
    _add_member(client, space["id"], created_site["id"], 1)

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE sites SET favicon_url = ? WHERE user_id = ? AND id = ?",
            ("javascript:alert(1)", user_id, created_site["id"]),
        )
        connection.commit()

    response = client.get(f"/api/spaces/{space['id']}")
    assert response.status_code == 200, response.text
    members = response.json()["members"]
    assert len(members) == 1
    assert members[0]["site"]["id"] == created_site["id"]
    assert members[0]["site"]["favicon_url"] is None


def test_space_mutations_claim_expected_version_atomically(
    isolated_spaces: tuple[TestClient, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, database_path = isolated_spaces
    user_id, _ = _register(client, "alice")
    alpha = _site(client, "Alpha")
    bravo = _site(client, "Bravo")
    charlie = _site(client, "Charlie")
    space = _space(client, "Concurrent mutations")
    _add_member(client, space["id"], alpha["id"], 1)
    database = Database(f"sqlite+aiosqlite:///{database_path.as_posix()}")

    async def exercise_races() -> dict[str, list[tuple[str, str | None, object | None]]]:
        async def run_pair(first, second):
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
            try:
                return list(await asyncio.gather(first(), second()))
            finally:
                monkeypatch.setattr(spaces_service, "_owned_space", original_owned_space)

        async def rename(name: str) -> tuple[str, str | None, object | None]:
            async with database.sessions() as session:
                try:
                    result = await spaces_service.update_space(
                        session,
                        user_id,
                        str(space["id"]),
                        SpaceUpdateRequest(expected_version=2, name=name),
                    )
                except spaces_service.SpaceConflictError as error:
                    return "conflict", error.code, None
                return "updated", None, result.version

        rename_outcomes = await run_pair(
            lambda: rename("First name"),
            lambda: rename("Second name"),
        )

        async def add(site_id: str) -> tuple[str, str | None, object | None]:
            async with database.sessions() as session:
                try:
                    result = await spaces_service.add_member(
                        session,
                        user_id,
                        str(space["id"]),
                        SpaceMemberAddRequest(expected_version=3, site_id=site_id),
                    )
                except spaces_service.SpaceConflictError as error:
                    return "conflict", error.code, None
                return "updated", None, result.member.site.id

        add_outcomes = await run_pair(
            lambda: add(str(bravo["id"])),
            lambda: add(str(charlie["id"])),
        )
        added_site_id = str(next(outcome[2] for outcome in add_outcomes if outcome[0] == "updated"))

        async def remove(site_id: str) -> tuple[str, str | None, object | None]:
            async with database.sessions() as session:
                try:
                    result = await spaces_service.remove_member(
                        session,
                        user_id,
                        str(space["id"]),
                        site_id,
                        expected_version=4,
                    )
                except spaces_service.SpaceConflictError as error:
                    return "conflict", error.code, None
                return "updated", None, result.version

        remove_outcomes = await run_pair(
            lambda: remove(str(alpha["id"])),
            lambda: remove(added_site_id),
        )

        async def delete_once() -> tuple[str, str | None, object | None]:
            async with database.sessions() as session:
                try:
                    result = await spaces_service.delete_space(
                        session,
                        user_id,
                        str(space["id"]),
                        expected_version=5,
                    )
                except spaces_service.SpaceConflictError as error:
                    return "conflict", error.code, None
                return "deleted", None, result.space_id

        delete_outcomes = await run_pair(delete_once, delete_once)
        return {
            "rename": rename_outcomes,
            "add": add_outcomes,
            "remove": remove_outcomes,
            "delete": delete_outcomes,
        }

    async def run_and_dispose() -> dict[str, list[tuple[str, str | None, object | None]]]:
        try:
            return await exercise_races()
        finally:
            await database.dispose()

    outcomes = asyncio.run(run_and_dispose())

    assert sorted(item[0] for item in outcomes["rename"]) == ["conflict", "updated"]
    assert sorted(item[0] for item in outcomes["add"]) == ["conflict", "updated"]
    assert sorted(item[0] for item in outcomes["remove"]) == ["conflict", "updated"]
    assert sorted(item[0] for item in outcomes["delete"]) == ["conflict", "deleted"]
    for operation_outcomes in outcomes.values():
        conflict = next(item for item in operation_outcomes if item[0] == "conflict")
        assert conflict[1] == "version_conflict"
    missing = client.get(f"/api/spaces/{space['id']}")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "not_found"


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
