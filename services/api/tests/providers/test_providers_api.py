from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient

from webhub.config import Settings
from webhub.db.migrations import upgrade_database
from webhub.main import create_app

COOKIE_NAME = "webhub_session"
ORIGIN = {"Origin": "http://testserver"}
MASTER_KEY = b"provider-test-master-key-32bytes"


@contextmanager
def _client(
    tmp_path: Path,
    *,
    master_key: bytes | None = MASTER_KEY,
    test_attempts: int = 10,
) -> Iterator[tuple[TestClient, Path]]:
    database_path = tmp_path / "main.sqlite3"
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        data_directory=tmp_path,
        provider_master_key=master_key,
        provider_test_rate_limit_attempts=test_attempts,
    )
    upgrade_database(settings.database_url)
    with TestClient(create_app(settings=settings)) as client:
        yield client, database_path


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


def _create_model(
    client: TestClient,
    *,
    name: str,
    secret: str = "sk-provider-secret",
    enabled: bool = False,
) -> dict[str, object]:
    response = client.post(
        "/api/providers",
        json={
            "kind": "model",
            "provider": "openai",
            "display_name": name,
            "model_name": "gpt-example",
            "secret": {"action": "write", "value": secret},
            "enabled": enabled,
        },
        headers=ORIGIN,
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_registry_auth_origin_and_exact_provider_scope(tmp_path: Path) -> None:
    with _client(tmp_path) as (client, _):
        assert client.get("/api/providers").status_code == 401
        assert client.get("/api/providers/registry").status_code == 401
        _register(client, "alice")

        registry = client.get("/api/providers/registry")
        assert registry.status_code == 200
        items = registry.json()["items"]
        assert {item["provider"] for item in items} == {
            "openai",
            "deepseek",
            "qwen",
            "kimi",
            "ollama",
            "openai_compatible",
            "tavily",
            "jina",
            "exa",
        }
        assert all(item["connection_test_supported"] is False for item in items)
        assert next(item for item in items if item["provider"] == "tavily")[
            "kinds"
        ] == ["search"]

        missing_origin = client.post(
            "/api/providers",
            json={
                "kind": "model",
                "provider": "ollama",
                "display_name": "Local",
                "base_url": "http://127.0.0.1:11434",
                "model_name": "qwen-example",
            },
        )
        assert missing_origin.status_code == 403
        assert client.get("/api/providers").json() == {"items": []}


def test_secret_is_encrypted_masked_replaced_and_cleared(tmp_path: Path) -> None:
    first_secret = "sk-first-never-return-this"
    second_secret = "sk-second-never-return-this"
    with _client(tmp_path) as (client, database_path):
        _register(client, "alice")
        created = _create_model(
            client,
            name="Primary model",
            secret=first_secret,
            enabled=True,
        )
        assert created["has_secret"] is True
        assert created["secret_mask"] == "********"
        assert created["enabled"] is True
        assert first_secret not in str(created)

        with sqlite3.connect(database_path) as connection:
            stored = connection.execute(
                "SELECT secret_ciphertext, secret_nonce, key_version, version "
                "FROM provider_configs WHERE id = ?",
                (created["id"],),
            ).fetchone()
        assert stored is not None
        assert first_secret.encode() not in stored[0]
        assert len(stored[1]) == 12
        assert stored[2:] == (1, 1)

        replaced = client.patch(
            f"/api/providers/{created['id']}",
            json={
                "expectedVersion": created["version"],
                "secret": {"action": "replace", "value": second_secret},
            },
            headers=ORIGIN,
        )
        assert replaced.status_code == 200, replaced.text
        assert replaced.json()["version"] == 2
        assert second_secret not in replaced.text

        tested = client.post(
            "/api/providers/test-connection",
            json={
                "config_id": created["id"],
                "expectedVersion": replaced.json()["version"],
            },
            headers=ORIGIN,
        )
        assert tested.status_code == 200, tested.text
        assert tested.json()["status"] == "unsupported"
        assert tested.json()["code"] == "connection_test_unsupported"
        assert "未发送任何外部请求" in tested.json()["message"]
        assert second_secret not in tested.text

        cleared = client.patch(
            f"/api/providers/{created['id']}",
            json={
                "expected_version": replaced.json()["version"],
                "secret": {"action": "clear"},
            },
            headers=ORIGIN,
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["has_secret"] is False
        assert cleared.json()["secret_mask"] is None
        assert cleared.json()["enabled"] is False
        with sqlite3.connect(database_path) as connection:
            assert connection.execute(
                "SELECT secret_ciphertext, secret_nonce FROM provider_configs WHERE id = ?",
                (created["id"],),
            ).fetchone() == (None, None)


def test_account_isolation_optimistic_lock_and_single_enabled_per_kind(
    tmp_path: Path,
) -> None:
    with _client(tmp_path) as (client, database_path):
        alice_token = _register(client, "alice")
        first = _create_model(client, name="First", enabled=True)
        second = _create_model(client, name="Second", enabled=True)

        configs = client.get("/api/providers", params={"kind": "model"}).json()["items"]
        assert sum(config["enabled"] for config in configs) == 1
        assert next(config for config in configs if config["id"] == first["id"])[
            "version"
        ] == 2
        assert next(config for config in configs if config["id"] == second["id"])[
            "enabled"
        ] is True

        stale = client.patch(
            f"/api/providers/{first['id']}",
            json={"expectedVersion": first["version"], "display_name": "Stale"},
            headers=ORIGIN,
        )
        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == "version_conflict"

        bob_token = _register(client, "bob")
        assert bob_token != alice_token
        assert client.get(f"/api/providers/{second['id']}").status_code == 404
        assert (
            client.patch(
                f"/api/providers/{second['id']}",
                json={"expectedVersion": second["version"], "enabled": False},
                headers=ORIGIN,
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/api/providers/test-connection",
                json={
                    "config_id": second["id"],
                    "expectedVersion": second["version"],
                },
                headers=ORIGIN,
            ).status_code
            == 404
        )
        assert (
            client.delete(
                f"/api/providers/{second['id']}",
                params={"expected_version": second["version"]},
                headers=ORIGIN,
            ).status_code
            == 404
        )
        assert client.get("/api/providers").json() == {"items": []}

        _use_token(client, alice_token)
        current = client.get(f"/api/providers/{second['id']}").json()
        deleted = client.delete(
            f"/api/providers/{second['id']}",
            params={"expected_version": current["version"]},
            headers=ORIGIN,
        )
        assert deleted.status_code == 200
        with sqlite3.connect(database_path) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM provider_configs WHERE enabled = 1 "
                "GROUP BY user_id, kind HAVING COUNT(*) > 1"
            ).fetchall() == []


def test_kind_validation_embedding_and_private_url_policy(tmp_path: Path) -> None:
    with _client(tmp_path) as (client, _):
        _register(client, "alice")
        embedding = client.post(
            "/api/providers",
            json={
                "kind": "embedding",
                "provider": "openai",
                "display_name": "Embedding",
                "model_name": "text-embedding-example",
                "secret": {"action": "write", "value": "sk-embedding"},
            },
            headers=ORIGIN,
        )
        assert embedding.status_code == 201, embedding.text

        ollama = client.post(
            "/api/providers",
            json={
                "kind": "model",
                "provider": "ollama",
                "display_name": "LAN Ollama",
                "base_url": "http://192.168.1.20:11434/",
                "model_name": "qwen-example",
                "enabled": True,
            },
            headers=ORIGIN,
        )
        assert ollama.status_code == 201, ollama.text
        assert ollama.json()["base_url"] == "http://192.168.1.20:11434"
        assert ollama.json()["has_secret"] is False

        wrong_kind = client.post(
            "/api/providers",
            json={
                "kind": "search",
                "provider": "openai",
                "display_name": "Wrong",
            },
            headers=ORIGIN,
        )
        assert wrong_kind.status_code == 422
        assert wrong_kind.json()["detail"]["code"] == "unsupported_provider"

        private_custom = client.post(
            "/api/providers",
            json={
                "kind": "model",
                "provider": "openai_compatible",
                "display_name": "Unsafe",
                "base_url": "https://127.0.0.1/v1",
                "model_name": "model",
            },
            headers=ORIGIN,
        )
        assert private_custom.status_code == 422
        assert private_custom.json()["detail"]["code"] == "unsafe_provider_target"

        search_model = client.post(
            "/api/providers",
            json={
                "kind": "search",
                "provider": "tavily",
                "display_name": "Search",
                "model_name": "not-allowed",
            },
            headers=ORIGIN,
        )
        assert search_model.status_code == 422


def test_missing_master_key_blocks_secret_paths_but_not_other_api(tmp_path: Path) -> None:
    with _client(tmp_path, master_key=None) as (client, _):
        _register(client, "alice")
        blocked = client.post(
            "/api/providers",
            json={
                "kind": "model",
                "provider": "openai",
                "display_name": "Blocked secret",
                "model_name": "gpt-example",
                "secret": {"action": "write", "value": "must-not-persist"},
            },
            headers=ORIGIN,
        )
        assert blocked.status_code == 503
        assert blocked.json()["detail"]["code"] == "provider_key_unavailable"
        assert client.get("/api/providers").json() == {"items": []}

        local = client.post(
            "/api/providers",
            json={
                "kind": "model",
                "provider": "ollama",
                "display_name": "Local metadata",
                "base_url": "http://127.0.0.1:11434",
                "model_name": "local-model",
            },
            headers=ORIGIN,
        )
        assert local.status_code == 201, local.text
        assert client.get("/api/auth/me").status_code == 200

        test_blocked = client.post(
            "/api/providers/test-connection",
            json={
                "config_id": local.json()["id"],
                "expectedVersion": local.json()["version"],
            },
            headers=ORIGIN,
        )
        assert test_blocked.status_code == 503
        assert test_blocked.json()["detail"]["code"] == "provider_key_unavailable"


def test_candidate_test_does_not_persist_secret_and_is_rate_limited(tmp_path: Path) -> None:
    test_secret = "candidate-only-secret"
    with _client(tmp_path, test_attempts=2) as (client, database_path):
        _register(client, "alice")
        payload = {
            "kind": "model",
            "provider": "openai",
            "model_name": "gpt-example",
            "secret": {"action": "test", "value": test_secret},
        }
        first = client.post(
            "/api/providers/test-connection",
            json=payload,
            headers=ORIGIN,
        )
        second = client.post(
            "/api/providers/test-connection",
            json=payload,
            headers=ORIGIN,
        )
        limited = client.post(
            "/api/providers/test-connection",
            json=payload,
            headers=ORIGIN,
        )
        assert first.status_code == second.status_code == 200
        assert limited.status_code == 429
        assert limited.json()["detail"]["code"] == "provider_test_rate_limited"
        assert int(limited.headers["Retry-After"]) >= 1
        assert test_secret not in first.text
        with sqlite3.connect(database_path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM provider_configs").fetchone() == (0,)
