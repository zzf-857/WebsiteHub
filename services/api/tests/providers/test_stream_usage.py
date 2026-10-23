from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from webhub.providers.registry import PROVIDER_REGISTRY
from webhub.providers.stream_usage import provider_config_values, stream_usage_enabled

from .test_providers_api import ORIGIN, _client, _register, _use_token


def _payload(kind: str = "model", provider: str = "openai_compatible") -> dict[str, object]:
    return {
        "kind": kind,
        "provider": provider,
        "display_name": "流式用量测试",
        "base_url": "https://llm.example.com/v1",
        "enabled": False,
    }


def test_compatible_stream_usage_create_update_read_and_preserve_other_settings(
    tmp_path: Path,
) -> None:
    with _client(tmp_path) as (client, database_path):
        _register(client, "alice")
        created = client.post(
            "/api/providers", json={**_payload(), "stream_usage": True}, headers=ORIGIN
        )
        assert created.status_code == 201, created.text
        config = created.json()
        assert config["stream_usage"] is True
        with sqlite3.connect(database_path) as connection:
            stored = json.loads(connection.execute(
                "SELECT config_json FROM provider_configs WHERE id = ?", (config["id"],)
            ).fetchone()[0])
            assert stored == {"stream_usage": True}
            connection.execute(
                "UPDATE provider_configs SET config_json = ? WHERE id = ?",
                (json.dumps({**stored, "other_option": "retained"}), config["id"]),
            )
        assert client.get(f"/api/providers/{config['id']}").json()["stream_usage"] is True
        assert client.get("/api/providers").json()["items"][0]["stream_usage"] is True
        updated = client.patch(
            f"/api/providers/{config['id']}",
            json={"expected_version": config["version"], "stream_usage": False},
            headers=ORIGIN,
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["version"] == config["version"] + 1
        assert updated.json()["stream_usage"] is False
        with sqlite3.connect(database_path) as connection:
            stored = json.loads(connection.execute(
                "SELECT config_json FROM provider_configs WHERE id = ?", (config["id"],)
            ).fetchone()[0])
        assert stored == {"stream_usage": False, "other_option": "retained"}
        unchanged = client.patch(
            f"/api/providers/{config['id']}",
            json={
                "expected_version": updated.json()["version"],
                "display_name": "重命名",
                "stream_usage": None,
            },
            headers=ORIGIN,
        )
        assert unchanged.status_code == 200, unchanged.text
        assert unchanged.json()["stream_usage"] is False
        stale = client.patch(
            f"/api/providers/{config['id']}",
            json={"expected_version": config["version"], "stream_usage": True},
            headers=ORIGIN,
        )
        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == "version_conflict"


@pytest.mark.parametrize(
    ("kind", "provider"),
    [
        (kind, definition.provider)
        for definition in PROVIDER_REGISTRY
        for kind in sorted(definition.kinds)
        if (kind, definition.provider) != ("model", "openai_compatible")
    ],
)
def test_stream_usage_rejected_for_every_other_kind_and_provider(
    tmp_path: Path, kind: str, provider: str,
) -> None:
    with _client(tmp_path) as (client, _):
        _register(client, "alice")
        payload = _payload(kind, provider)
        # 固定端点不接收自定义地址；用官方默认地址保持其余输入合法。
        definition = next(item for item in PROVIDER_REGISTRY if item.provider == provider)
        if definition.fixed_base_url:
            payload.pop("base_url")
        expected = {"code": "stream_usage_not_applicable", "message": "该服务商不支持此选项"}
        for value in (True, False):
            rejected = client.post(
                "/api/providers", json={**payload, "stream_usage": value}, headers=ORIGIN
            )
            assert rejected.status_code == 422
            assert rejected.json()["detail"] == expected
        created = client.post("/api/providers", json=payload, headers=ORIGIN)
        assert created.status_code == 201, created.text
        config = created.json()
        assert config["stream_usage"] is (provider in {"openai", "deepseek"})
        before = client.get(f"/api/providers/{config['id']}").json()
        rejected = client.patch(
            f"/api/providers/{config['id']}",
            json={"expected_version": config["version"], "stream_usage": False},
            headers=ORIGIN,
        )
        assert rejected.status_code == 422
        assert rejected.json()["detail"] == expected
        assert client.get(f"/api/providers/{config['id']}").json() == before


def test_stream_usage_default_auth_origin_scope_and_strict_boolean(tmp_path: Path) -> None:
    with _client(tmp_path) as (client, _):
        assert client.post("/api/providers", json=_payload(), headers=ORIGIN).status_code == 401
        alice = _register(client, "alice")
        assert client.post("/api/providers", json=_payload()).status_code == 403
        created = client.post("/api/providers", json=_payload(), headers=ORIGIN)
        assert created.status_code == 201, created.text
        config = created.json()
        assert config["stream_usage"] is False
        for value in ("true", 1, [], {}):
            rejected = client.patch(
                f"/api/providers/{config['id']}",
                json={"expected_version": config["version"], "stream_usage": value},
                headers=ORIGIN,
            )
            assert rejected.status_code == 422
        _register(client, "bob")
        assert client.get(f"/api/providers/{config['id']}").status_code == 404
        hidden = client.patch(
            f"/api/providers/{config['id']}",
            json={"expected_version": config["version"], "stream_usage": True},
            headers=ORIGIN,
        )
        assert hidden.status_code == 404
        _use_token(client, alice)
        assert client.get(f"/api/providers/{config['id']}").json()["stream_usage"] is False


@pytest.mark.parametrize("provider", ["openai", "deepseek", "openai_compatible", "ollama"])
def test_stream_usage_rule_ignores_non_boolean_settings(provider: str) -> None:
    for value in (None, False, "true", 1, []):
        assert stream_usage_enabled(provider, {"stream_usage": value}) is (
            provider in {"openai", "deepseek"}
        )
    assert stream_usage_enabled(provider, {"stream_usage": True}) is (
        provider in {"openai", "deepseek", "openai_compatible"}
    )
    for damaged in ("not-json", "[]", "null"):
        assert provider_config_values(damaged) == {}
