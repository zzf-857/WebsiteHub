import asyncio
import hashlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from webhub.auth.cli import reset_local_password
from webhub.config import Settings
from webhub.db.migrations import upgrade_database
from webhub.main import create_app

COOKIE_NAME = "webhub_session"
SAME_ORIGIN_HEADERS = {"Origin": "http://testserver"}


def _settings(tmp_path: Path) -> Settings:
    database_path = tmp_path / "main.sqlite3"
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        data_directory=tmp_path,
    )
    upgrade_database(settings.database_url)
    return settings


@pytest.fixture
def auth_client(tmp_path: Path) -> Iterator[tuple[TestClient, Path]]:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings=settings)) as client:
        yield client, tmp_path / "main.sqlite3"


def _register(
    client: TestClient,
    username: str = "alice",
    password: str = "correct horse battery staple",
) -> tuple[dict[str, object], str]:
    response = client.post(
        "/api/auth/register",
        json={"username": username, "password": password, "display_name": "Alice"},
        headers=SAME_ORIGIN_HEADERS,
    )
    assert response.status_code == 201, response.text
    token = response.cookies.get(COOKIE_NAME)
    assert token
    return response.json(), token


def _use_token(client: TestClient, token: str) -> None:
    client.cookies.clear()
    client.cookies.set(COOKIE_NAME, token)


def test_register_me_logout_and_login_round_trip(
    auth_client: tuple[TestClient, Path],
) -> None:
    client, _ = auth_client
    payload, _ = _register(client, username="Alice")

    assert payload["user"]["username"] == "alice"
    assert payload["user"]["display_name"] == "Alice"
    assert payload["user"]["preferences"] == {"theme": "system", "locale": "zh-CN"}
    assert "password" not in response_text(payload)

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["id"] == payload["user"]["id"]

    rejected_logout = client.post("/api/auth/logout", headers={"Origin": "http://attacker.invalid"})
    assert rejected_logout.status_code == 403
    assert client.get("/api/auth/me").status_code == 200

    logout = client.post("/api/auth/logout", headers=SAME_ORIGIN_HEADERS)
    assert logout.status_code == 204
    assert client.get("/api/auth/me").status_code == 401

    login = client.post(
        "/api/auth/login",
        json={"username": "ALICE", "password": "correct horse battery staple"},
        headers=SAME_ORIGIN_HEADERS,
    )
    assert login.status_code == 200
    assert client.get("/api/auth/me").status_code == 200


def response_text(payload: object) -> str:
    return repr(payload).casefold()


def test_canonical_username_is_unique(auth_client: tuple[TestClient, Path]) -> None:
    client, _ = auth_client
    _register(client, username="Alice")
    client.cookies.clear()

    duplicate = client.post(
        "/api/auth/register",
        json={"username": "ＡＬＩＣＥ", "password": "another secure password"},
        headers=SAME_ORIGIN_HEADERS,
    )

    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "用户名已被使用"


def test_password_change_revokes_other_sessions(auth_client: tuple[TestClient, Path]) -> None:
    client, _ = auth_client
    _, first_token = _register(client)
    client.cookies.clear()
    second_login = client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "correct horse battery staple"},
        headers=SAME_ORIGIN_HEADERS,
    )
    second_token = second_login.cookies.get(COOKIE_NAME)
    assert second_token

    _use_token(client, first_token)
    changed = client.post(
        "/api/auth/change-password",
        json={
            "current_password": "correct horse battery staple",
            "new_password": "a newly secured password",
        },
        headers=SAME_ORIGIN_HEADERS,
    )
    assert changed.status_code == 200
    assert client.get("/api/auth/me").status_code == 200

    _use_token(client, second_token)
    assert client.get("/api/auth/me").status_code == 401
    old_login = client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "correct horse battery staple"},
        headers=SAME_ORIGIN_HEADERS,
    )
    assert old_login.status_code == 401
    new_login = client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "a newly secured password"},
        headers=SAME_ORIGIN_HEADERS,
    )
    assert new_login.status_code == 200


def test_preferences_and_sessions_are_isolated_by_account(
    auth_client: tuple[TestClient, Path],
) -> None:
    client, _ = auth_client
    alice, alice_token = _register(client, username="alice")
    client.cookies.clear()
    bob, bob_token = _register(client, username="bob")

    _use_token(client, alice_token)
    update = client.patch(
        "/api/auth/preferences",
        json={"theme": "dark"},
        headers=SAME_ORIGIN_HEADERS,
    )
    assert update.status_code == 200
    assert update.json()["user"]["preferences"]["theme"] == "dark"

    _use_token(client, bob_token)
    bob_me = client.get("/api/auth/me")
    assert bob_me.status_code == 200
    assert bob_me.json()["user"]["id"] == bob["user"]["id"]
    assert bob_me.json()["user"]["id"] != alice["user"]["id"]
    assert bob_me.json()["user"]["preferences"]["theme"] == "system"


def test_database_stores_only_session_hash_and_argon2_password(
    auth_client: tuple[TestClient, Path],
) -> None:
    client, database_path = auth_client
    _, raw_token = _register(client)

    with sqlite3.connect(database_path) as connection:
        password_hash = connection.execute("SELECT password_hash FROM users").fetchone()[0]
        stored_token = connection.execute("SELECT token_hash FROM login_sessions").fetchone()[0]

    assert password_hash.startswith("$argon2id$")
    assert stored_token == hashlib.sha256(raw_token.encode()).hexdigest()
    assert raw_token not in stored_token


def test_login_rate_limit_blocks_repeated_failures(auth_client: tuple[TestClient, Path]) -> None:
    client, _ = auth_client
    _register(client)
    client.cookies.clear()

    for _ in range(5):
        response = client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "wrong password"},
            headers=SAME_ORIGIN_HEADERS,
        )
        assert response.status_code == 401

    limited = client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "correct horse battery staple"},
        headers=SAME_ORIGIN_HEADERS,
    )
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) > 0


def test_account_survives_application_restart(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings=settings)) as first_client:
        registered, token = _register(first_client)
        update = first_client.patch(
            "/api/auth/preferences",
            json={"theme": "dark"},
            headers=SAME_ORIGIN_HEADERS,
        )
        assert update.status_code == 200

    with TestClient(create_app(settings=settings)) as second_client:
        _use_token(second_client, token)
        restored = second_client.get("/api/auth/me")
        assert restored.status_code == 200
        assert restored.json()["user"]["id"] == registered["user"]["id"]
        assert restored.json()["user"]["preferences"]["theme"] == "dark"

        second_client.cookies.clear()
        login = second_client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
            headers=SAME_ORIGIN_HEADERS,
        )
        assert login.status_code == 200
        assert second_client.get("/api/auth/me").status_code == 200


def test_local_password_reset_revokes_every_existing_session(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings=settings)) as first_client:
        _, old_token = _register(first_client)

    asyncio.run(reset_local_password(settings.database_url, "ALICE", "a replacement password"))

    with TestClient(create_app(settings=settings)) as second_client:
        _use_token(second_client, old_token)
        assert second_client.get("/api/auth/me").status_code == 401

        old_login = second_client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
            headers=SAME_ORIGIN_HEADERS,
        )
        new_login = second_client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "a replacement password"},
            headers=SAME_ORIGIN_HEADERS,
        )

    assert old_login.status_code == 401
    assert new_login.status_code == 200
