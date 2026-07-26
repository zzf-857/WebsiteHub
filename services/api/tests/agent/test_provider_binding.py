from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from webhub.agent import provider_binding as binding_module
from webhub.agent.provider_binding import ProviderBinding, resolve_binding
from webhub.agent.runner import AgentProviderNotConfiguredError
from webhub.config import Settings
from webhub.db.database import Database
from webhub.db.migrations import upgrade_database
from webhub.db.models import User
from webhub.main import create_app

COOKIE_NAME = "webhub_session"
ORIGIN = {"Origin": "http://testserver"}
MASTER_KEY = b"provider-test-master-key-32bytes"
OLLAMA_BASE_URL = "http://127.0.0.1:11434"


@contextmanager
def _client(tmp_path: Path) -> Iterator[tuple[TestClient, Settings]]:
    database_path = tmp_path / "main.sqlite3"
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        data_directory=tmp_path,
        provider_master_key=MASTER_KEY,
    )
    upgrade_database(settings.database_url)
    with TestClient(create_app(settings=settings)) as client:
        client.post(
            "/api/auth/register",
            json={"username": "alice", "password": "a sufficiently secure password"},
            headers=ORIGIN,
        )
        yield client, settings


def _resolve(settings: Settings, kind: str = "model") -> ProviderBinding:
    """Resolve against a private Database so the app's event loop stays clean."""

    async def scenario() -> ProviderBinding:
        database = Database(settings.database_url)
        try:
            async with database.sessions() as session:
                user_id = await session.scalar(select(User.id))
                assert user_id is not None
                return await resolve_binding(
                    session,
                    settings,
                    user_id=user_id,
                    kind=kind,  # type: ignore[arg-type]
                )
        finally:
            await database.dispose()

    return asyncio.run(scenario())


def test_enabled_ollama_config_resolves_without_a_secret(tmp_path: Path) -> None:
    with _client(tmp_path) as (client, settings):
        response = client.post(
            "/api/providers",
            json={
                "kind": "model",
                "provider": "ollama",
                "display_name": "本地 Ollama",
                "base_url": OLLAMA_BASE_URL,
                "model_name": "qwen3",
                "enabled": True,
            },
            headers=ORIGIN,
        )
        assert response.status_code == 201, response.text

    binding = _resolve(settings)

    assert binding.provider == "ollama"
    assert binding.model_name == "qwen3"
    # Ollama stores the root but only speaks OpenAI protocol under /v1.
    assert binding.base_url == f"{OLLAMA_BASE_URL}/v1"
    assert binding.api_key is None
    assert binding.client_api_key  # SDK clients reject an empty key


def test_binding_repr_never_exposes_the_decrypted_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def allow_target(*args: object, **kwargs: object) -> None:
        del args, kwargs

    monkeypatch.setattr(binding_module, "validate_connection_target", allow_target)

    with _client(tmp_path) as (client, settings):
        response = client.post(
            "/api/providers",
            json={
                "kind": "model",
                "provider": "deepseek",
                "display_name": "DeepSeek",
                "model_name": "deepseek-chat",
                "secret": {"action": "write", "value": "sk-super-secret-value"},
                "enabled": True,
            },
            headers=ORIGIN,
        )
        assert response.status_code == 201, response.text

    binding = _resolve(settings)

    assert binding.api_key == "sk-super-secret-value"
    assert binding.base_url == "https://api.deepseek.com/v1"
    assert "sk-super-secret-value" not in repr(binding)
    assert "sk-super-secret-value" not in str(binding)


def test_disabled_or_absent_config_is_reported_as_not_configured(tmp_path: Path) -> None:
    with _client(tmp_path) as (client, settings):
        response = client.post(
            "/api/providers",
            json={
                "kind": "model",
                "provider": "ollama",
                "display_name": "未启用",
                "base_url": OLLAMA_BASE_URL,
                "model_name": "qwen3",
                "enabled": False,
            },
            headers=ORIGIN,
        )
        assert response.status_code == 201, response.text

    with pytest.raises(AgentProviderNotConfiguredError):
        _resolve(settings)


def test_unsafe_base_url_is_refused_at_call_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from webhub.providers.targets import ProviderTargetError

    async def reject_target(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise ProviderTargetError("unsafe_provider_target", "指向不安全目标")

    monkeypatch.setattr(binding_module, "validate_connection_target", reject_target)

    with _client(tmp_path) as (client, settings):
        response = client.post(
            "/api/providers",
            json={
                "kind": "model",
                "provider": "ollama",
                "display_name": "本地 Ollama",
                "base_url": OLLAMA_BASE_URL,
                "model_name": "qwen3",
                "enabled": True,
            },
            headers=ORIGIN,
        )
        assert response.status_code == 201, response.text

    # A hostname that passed validation at save time can be re-pointed later,
    # so the runner must not trust the stored value.
    with pytest.raises(AgentProviderNotConfiguredError):
        _resolve(settings)


def test_another_accounts_config_is_never_borrowed(tmp_path: Path) -> None:
    with _client(tmp_path) as (client, settings):
        response = client.post(
            "/api/providers",
            json={
                "kind": "model",
                "provider": "ollama",
                "display_name": "本地 Ollama",
                "base_url": OLLAMA_BASE_URL,
                "model_name": "qwen3",
                "enabled": True,
            },
            headers=ORIGIN,
        )
        assert response.status_code == 201, response.text

        client.cookies.clear()
        registered = client.post(
            "/api/auth/register",
            json={"username": "bob", "password": "another secure password here"},
            headers=ORIGIN,
        )
        assert registered.status_code == 201, registered.text

    async def scenario() -> None:
        database = Database(settings.database_url)
        try:
            async with database.sessions() as session:
                bob_id = await session.scalar(select(User.id).where(User.username == "bob"))
                assert bob_id is not None
                await resolve_binding(session, settings, user_id=bob_id, kind="model")
        finally:
            await database.dispose()

    with pytest.raises(AgentProviderNotConfiguredError):
        asyncio.run(scenario())
