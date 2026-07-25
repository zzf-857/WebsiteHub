import asyncio
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from webhub.config import Settings
from webhub.db.database import Database
from webhub.db.migrations import upgrade_database
from webhub.library import service as library_service
from webhub.library.schemas import SiteUpdateRequest
from webhub.main import create_app

COOKIE_NAME = "webhub_session"
ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture
def isolated_library(tmp_path: Path) -> Iterator[tuple[TestClient, Path]]:
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
    assert response.status_code == 201
    token = response.cookies.get(COOKIE_NAME)
    assert token
    return str(response.json()["user"]["id"]), token


def _use_token(client: TestClient, token: str) -> None:
    client.cookies.clear()
    client.cookies.set(COOKIE_NAME, token)


def test_cross_account_ids_are_hidden_and_relationship_tampering_is_rejected(
    isolated_library: tuple[TestClient, Path],
) -> None:
    client, database_path = isolated_library
    alice_id, alice_token = _register(client, "alice")
    alice_category = client.post(
        "/api/library/categories", json={"name": "Alice only"}, headers=ORIGIN
    ).json()
    alice_tag = client.post(
        "/api/library/tags", json={"name": "Alice tag"}, headers=ORIGIN
    ).json()
    alice_site = client.post(
        "/api/library/sites",
        json={
            "name": "Alice site",
            "url": "https://shared.example.com/path?q=1#one",
            "category_id": alice_category["id"],
            "tag_ids": [alice_tag["id"]],
        },
        headers=ORIGIN,
    ).json()

    client.cookies.clear()
    bob_id, bob_token = _register(client, "bob")
    bob_default = client.get("/api/library/categories").json()["items"][0]
    bob_tag = client.post(
        "/api/library/tags", json={"name": "Bob tag"}, headers=ORIGIN
    ).json()

    for method, path, kwargs in (
        ("get", f"/api/library/sites/{alice_site['id']}", {}),
        (
            "patch",
            f"/api/library/sites/{alice_site['id']}",
            {"json": {"expected_version": 1, "pinned": True}, "headers": ORIGIN},
        ),
        ("delete", f"/api/library/sites/{alice_site['id']}", {"headers": ORIGIN}),
        (
            "patch",
            f"/api/library/categories/{alice_category['id']}",
            {"json": {"name": "Stolen"}, "headers": ORIGIN},
        ),
        (
            "get",
            f"/api/library/categories/{alice_category['id']}/delete-preview",
            {},
        ),
        (
            "delete",
            f"/api/library/tags/{alice_tag['id']}",
            {"headers": ORIGIN},
        ),
    ):
        assert getattr(client, method)(path, **kwargs).status_code == 404

    wrong_category = client.post(
        "/api/library/sites",
        json={
            "name": "Wrong category",
            "url": "https://wrong-category.example.com",
            "category_id": alice_category["id"],
            "tag_ids": [],
        },
        headers=ORIGIN,
    )
    wrong_tag = client.post(
        "/api/library/sites",
        json={
            "name": "Wrong tag",
            "url": "https://wrong-tag.example.com",
            "category_id": bob_default["id"],
            "tag_ids": [alice_tag["id"]],
        },
        headers=ORIGIN,
    )
    assert wrong_category.status_code == 404
    assert wrong_tag.status_code == 404
    assert client.get(
        "/api/library/sites", params={"category_id": alice_category["id"]}
    ).status_code == 404
    assert client.get(
        "/api/library/sites", params={"tag_id": alice_tag["id"]}
    ).status_code == 404

    bob_copy = client.post(
        "/api/library/sites",
        json={
            "name": "Bob copy",
            "url": alice_site["identity_url"],
            "category_id": bob_default["id"],
            "tag_ids": [bob_tag["id"]],
        },
        headers=ORIGIN,
    )
    assert bob_copy.status_code == 201

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO site_tags(user_id, site_id, tag_id, created_at) "
                "VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
                (alice_id, alice_site["id"], bob_tag["id"]),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE sites SET category_id = ? WHERE user_id = ? AND id = ?",
                (bob_default["id"], alice_id, alice_site["id"]),
            )

    _use_token(client, alice_token)
    alice_restored = client.get(f"/api/library/sites/{alice_site['id']}")
    assert alice_restored.status_code == 200
    assert alice_restored.json()["category"]["id"] == alice_category["id"]
    assert alice_restored.json()["tags"] == [{"id": alice_tag["id"], "name": "Alice tag"}]
    _use_token(client, bob_token)
    assert client.get("/api/library/sites").json()["aggregate"]["matched_count"] == 1
    assert alice_id != bob_id


def test_site_cursor_is_bound_to_the_authenticated_account(
    isolated_library: tuple[TestClient, Path],
) -> None:
    client, _ = isolated_library
    _, alice_token = _register(client, "alice")
    for name in ("Alpha", "Mike"):
        created = client.post(
            "/api/library/sites",
            json={"name": name, "url": f"https://alice-{name.casefold()}.example.com"},
            headers=ORIGIN,
        )
        assert created.status_code == 201
    alice_page = client.get(
        "/api/library/sites",
        params={"sort": "name", "direction": "asc", "limit": 1},
    )
    alice_cursor = alice_page.json()["next_cursor"]
    assert alice_cursor

    client.cookies.clear()
    _register(client, "bob")
    for name in ("Bravo", "Zulu"):
        created = client.post(
            "/api/library/sites",
            json={"name": name, "url": f"https://bob-{name.casefold()}.example.com"},
            headers=ORIGIN,
        )
        assert created.status_code == 201

    cross_account_cursor = client.get(
        "/api/library/sites",
        params={
            "sort": "name",
            "direction": "asc",
            "limit": 1,
            "cursor": alice_cursor,
        },
    )
    assert cross_account_cursor.status_code == 422

    _use_token(client, alice_token)
    continued = client.get(
        "/api/library/sites",
        params={
            "sort": "name",
            "direction": "asc",
            "limit": 1,
            "cursor": alice_cursor,
        },
    )
    assert continued.status_code == 200
    assert [item["name"] for item in continued.json()["items"]] == ["Mike"]


def test_site_version_claim_is_atomic_across_concurrent_sessions(
    isolated_library: tuple[TestClient, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, database_path = isolated_library
    user_id, _ = _register(client, "alice")
    created = client.post(
        "/api/library/sites",
        json={"name": "Original", "url": "https://concurrent.example.com"},
        headers=ORIGIN,
    ).json()
    database = Database(f"sqlite+aiosqlite:///{database_path.as_posix()}")

    async def race_updates() -> list[tuple[str, str | None, int | None]]:
        original_owned_site = library_service._owned_site
        ready_count = 0
        ready_lock = asyncio.Lock()
        release = asyncio.Event()

        async def synchronized_owned_site(session, owner_id: str, site_id: str):
            nonlocal ready_count
            site = await original_owned_site(session, owner_id, site_id)
            async with ready_lock:
                ready_count += 1
                if ready_count == 2:
                    release.set()
            await asyncio.wait_for(release.wait(), timeout=5)
            return site

        monkeypatch.setattr(library_service, "_owned_site", synchronized_owned_site)

        async def update_name(name: str) -> tuple[str, str | None, int | None]:
            async with database.sessions() as session:
                try:
                    result = await library_service.update_site(
                        session,
                        user_id,
                        str(created["id"]),
                        SiteUpdateRequest(expected_version=1, name=name),
                    )
                except library_service.LibraryConflictError:
                    return "conflict", None, None
                return "updated", result.name, result.version

        try:
            return list(await asyncio.gather(update_name("First"), update_name("Second")))
        finally:
            await database.dispose()

    outcomes = asyncio.run(race_updates())
    assert sorted(outcome[0] for outcome in outcomes) == ["conflict", "updated"]
    winner = next(outcome for outcome in outcomes if outcome[0] == "updated")
    assert winner[2] == 2

    stored = client.get(f"/api/library/sites/{created['id']}")
    assert stored.status_code == 200
    assert stored.json()["name"] == winner[1]
    assert stored.json()["version"] == 2
