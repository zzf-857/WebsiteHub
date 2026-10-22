"""Q45: the optional per-account GitHub token as a ``code_host`` Provider."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from alembic import command
from webhub.agent.provider_binding import resolve_github_credential
from webhub.config import Settings
from webhub.db.database import Database
from webhub.db.migrations import create_alembic_config, upgrade_database
from webhub.ingestion.github import GitHubCredential
from webhub.main import create_app
from webhub.providers import service as provider_service
from webhub.providers.connectivity import (
    ProviderCodeHostProbeResult,
    ProviderProbeError,
    probe_code_host,
)
from webhub.providers.registry import provider_definition

ORIGIN = {"Origin": "http://testserver"}
MASTER_KEY = b"provider-test-master-key-32bytes"
TOKEN = "github_pat_never_return_this_value"
GITHUB = provider_definition("code_host", "github")
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _probe(
    handler: Callable[[httpx.Request], httpx.Response],
    monkeypatch: pytest.MonkeyPatch,
    *,
    definition=GITHUB,
) -> tuple[list[httpx.Request], object]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(record)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)  # type: ignore[arg-type]

    async def allow(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    monkeypatch.setattr("webhub.providers.connectivity.validate_connection_target", allow)
    try:
        outcome: object = asyncio.run(
            probe_code_host(
                definition,
                base_url="https://api.github.com",
                api_key=TOKEN,
                timeout_seconds=2,
            )
        )
    except ProviderProbeError as error:
        outcome = error
    return seen, outcome


def test_probe_reads_the_free_rate_limit_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "resources": {"core": {"limit": 5000, "remaining": 4987, "reset": 1}},
        "rate": {"limit": 5000, "remaining": 4987},
    }
    seen, outcome = _probe(lambda _request: httpx.Response(200, json=payload), monkeypatch)

    assert outcome == ProviderCodeHostProbeResult(limit=5000, remaining=4987)
    assert str(seen[0].url) == "https://api.github.com/rate_limit"
    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert seen[0].headers["accept"] == "application/vnd.github+json"
    assert seen[0].headers["x-github-api-version"] == "2022-11-28"


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(401, text=f"Bad credentials {TOKEN}"), "provider_auth_failed"),
        (httpx.Response(429), "provider_rate_limited"),
        (httpx.Response(200, json={"rate": {"limit": "lots"}}), "provider_response_invalid"),
        (httpx.Response(200, text="<html>"), "provider_response_invalid"),
    ],
)
def test_probe_failures_are_fixed_messages(
    monkeypatch: pytest.MonkeyPatch,
    response: httpx.Response,
    code: str,
) -> None:
    _, outcome = _probe(lambda _request: response, monkeypatch)

    assert isinstance(outcome, ProviderProbeError)
    assert outcome.code == code
    assert TOKEN not in outcome.message


def test_probe_refuses_a_non_code_host_definition(monkeypatch: pytest.MonkeyPatch) -> None:
    seen, outcome = _probe(
        lambda _request: httpx.Response(200, json={}),
        monkeypatch,
        definition=provider_definition("model", "openai"),
    )

    assert isinstance(outcome, ProviderProbeError)
    assert seen == []


@contextmanager
def _client(tmp_path: Path) -> Iterator[tuple[TestClient, Settings]]:
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'main.sqlite3').as_posix()}",
        data_directory=tmp_path,
        provider_master_key=MASTER_KEY,
    )
    upgrade_database(settings.database_url)
    with TestClient(create_app(settings=settings)) as client:
        yield client, settings


def _register(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/register",
        json={"username": username, "password": "a sufficiently secure password"},
        headers=ORIGIN,
    )
    assert response.status_code == 201, response.text


def _user_id(tmp_path: Path, username: str) -> str:
    with sqlite3.connect(tmp_path / "main.sqlite3") as connection:
        return str(
            connection.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()[0]
        )


def _credential(settings: Settings, user_id: str) -> GitHubCredential | None:
    async def scenario() -> GitHubCredential | None:
        database = Database(settings.database_url)
        try:
            async with database.sessions() as session:
                return await resolve_github_credential(session, settings, user_id=user_id)
        finally:
            await database.dispose()

    return asyncio.run(scenario())


def test_github_token_is_encrypted_masked_tested_and_resolved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probes: list[str | None] = []

    async def fake_probe(definition, *, base_url: str, api_key: str | None, timeout_seconds: int):
        assert definition.provider == "github"
        assert base_url == "https://api.github.com"
        probes.append(api_key)
        return ProviderCodeHostProbeResult(limit=5000, remaining=4321)

    monkeypatch.setattr(provider_service, "probe_code_host", fake_probe)
    with _client(tmp_path) as (client, settings):
        _register(client, "alice")
        alice = _user_id(tmp_path, "alice")
        assert _credential(settings, alice) is None

        rejected = client.post(
            "/api/providers",
            json={
                "kind": "code_host",
                "provider": "github",
                "display_name": "GitHub",
                "model_name": "gpt",
                "secret": {"action": "write", "value": TOKEN},
            },
            headers=ORIGIN,
        )
        assert rejected.status_code == 422

        missing_key = client.post(
            "/api/providers/test-connection",
            json={"kind": "code_host", "provider": "github"},
            headers=ORIGIN,
        )
        assert missing_key.status_code == 422
        assert probes == []

        created = client.post(
            "/api/providers",
            json={
                "kind": "code_host",
                "provider": "github",
                "display_name": "GitHub",
                # A fixed adapter never stores a caller-supplied address.
                "base_url": "https://evil.example",
                "secret": {"action": "write", "value": TOKEN},
                "enabled": True,
            },
            headers=ORIGIN,
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["kind"] == "code_host"
        assert body["base_url"] is None
        assert body["has_secret"] is True
        assert body["secret_mask"] == "********"
        assert TOKEN not in created.text
        listed = client.get("/api/providers?kind=code_host")
        assert [item["id"] for item in listed.json()["items"]] == [body["id"]]
        assert TOKEN not in listed.text
        with sqlite3.connect(tmp_path / "main.sqlite3") as connection:
            stored = connection.execute(
                "SELECT secret_ciphertext FROM provider_configs WHERE id = ?", (body["id"],)
            ).fetchone()[0]
        assert TOKEN.encode() not in stored

        tested = client.post(
            "/api/providers/test-connection",
            json={"config_id": body["id"], "expected_version": body["version"]},
            headers=ORIGIN,
        )
        assert tested.status_code == 200, tested.text
        assert tested.json()["status"] == "ok"
        assert "5000" in tested.json()["message"]
        assert "4321" in tested.json()["message"]
        assert TOKEN not in tested.text
        assert probes == [TOKEN]

        credential = _credential(settings, alice)
        assert credential == GitHubCredential(config_id=body["id"], token=TOKEN)
        assert TOKEN not in repr(credential)

        disabled = client.patch(
            f"/api/providers/{body['id']}",
            json={"expected_version": body["version"], "enabled": False},
            headers=ORIGIN,
        )
        assert disabled.status_code == 200, disabled.text
        assert _credential(settings, alice) is None

        # Another account never sees alice's token (registration switches
        # the client's session to bob).
        client.patch(
            f"/api/providers/{body['id']}",
            json={"expected_version": disabled.json()["version"], "enabled": True},
            headers=ORIGIN,
        )
        assert _credential(settings, alice) is not None
        _register(client, "bob")
        assert _credential(settings, _user_id(tmp_path, "bob")) is None


def test_migration_admits_code_host_and_downgrade_removes_it(tmp_path: Path) -> None:
    database_path = tmp_path / "kinds.sqlite3"
    config = create_alembic_config(f"sqlite+aiosqlite:///{database_path.as_posix()}")
    command.upgrade(config, "20261021_0021")

    def insert(config_id: str, kind: str) -> None:
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                "INSERT INTO provider_configs("
                "id, user_id, kind, provider, display_name, key_version, enabled, "
                "config_json, created_at, updated_at, version"
                ") VALUES (?, 'user', ?, 'github', ?, 1, 1, '{}', "
                "'2026-10-22 00:00:00+00:00', '2026-10-22 00:00:00+00:00', 1)",
                (config_id, kind, config_id),
            )

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO users(id, username, display_name, password_hash, is_active, "
            "created_at, updated_at) VALUES ('user', 'u', 'U', 'hash', 1, "
            "'2026-10-22 00:00:00+00:00', '2026-10-22 00:00:00+00:00')"
        )
    insert("model-config", "model")
    with pytest.raises(sqlite3.IntegrityError):
        insert("early", "code_host")

    command.upgrade(config, "20261022_0022")
    insert("github-config", "code_host")
    with sqlite3.connect(database_path) as connection:
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(provider_configs)")}
    assert "uq_provider_configs_enabled_per_user_kind" in indexes
    # The single-enabled guard survived the table rebuild.
    with pytest.raises(sqlite3.IntegrityError):
        insert("second-github", "code_host")
    with pytest.raises(sqlite3.IntegrityError):
        insert("bogus", "bogus")

    command.downgrade(config, "20261021_0021")
    with sqlite3.connect(database_path) as connection:
        kinds = [row[0] for row in connection.execute("SELECT kind FROM provider_configs")]
    assert kinds == ["model"]
