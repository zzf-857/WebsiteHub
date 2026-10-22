"""Q43 外部 Agent 接入：令牌、MCP 往返、权限分级、跨账号隔离与审计。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from alembic import command
from webhub.agent_access import tokens as token_service
from webhub.agent_access import tools as agent_tools
from webhub.config import Settings
from webhub.db.database import Database
from webhub.db.migrations import create_alembic_config, upgrade_database
from webhub.ingestion.worker import AnalysisSchedule
from webhub.library import service as library_service
from webhub.main import create_app

COOKIE_NAME = "webhub_session"
ORIGIN = {"Origin": "http://testserver"}
MCP_ACCEPT = "application/json, text/event-stream"
TOOL_NAMES = {
    "search_resources",
    "get_resource",
    "get_rediscover",
    "list_taxonomy",
    "save_resource",
    "update_resource",
    "mark_resource",
}


@dataclass
class Environment:
    client: TestClient
    database_path: Path
    database_url: str
    scheduled: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)


@pytest.fixture
def environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Environment]:
    database_path = tmp_path / "main.sqlite3"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    settings = Settings(environment="test", database_url=database_url, data_directory=tmp_path)
    upgrade_database(settings.database_url)
    scheduled: list[tuple[str, tuple[str, ...]]] = []

    def fake_schedule(*_args: object, **kwargs: Any) -> AnalysisSchedule:
        # Record intent only: real analysis needs the network.
        scheduled.append((str(kwargs["user_id"]), tuple(kwargs["site_ids"])))
        return AnalysisSchedule(queued=1, already_queued=0, rejected=0)

    monkeypatch.setattr("webhub.ingestion.worker.schedule_analysis", fake_schedule)
    monkeypatch.setattr(
        "webhub.ingestion.worker.ensure_auto_backfill",
        lambda *_args, **_kwargs: False,
    )
    with TestClient(create_app(settings=settings)) as client:
        yield Environment(client, database_path, database_url, scheduled)


# ------------------------------------------------------------------ helpers


def _register(client: TestClient, username: str) -> tuple[str, str]:
    response = client.post(
        "/api/auth/register",
        json={"username": username, "password": "a sufficiently secure password"},
        headers=ORIGIN,
    )
    assert response.status_code == 201, response.text
    cookie = response.cookies.get(COOKIE_NAME)
    assert cookie
    return str(response.json()["user"]["id"]), cookie


def _use_cookie(client: TestClient, cookie: str) -> None:
    client.cookies.clear()
    client.cookies.set(COOKIE_NAME, cookie)


def _create_token(
    client: TestClient,
    *,
    name: str = "Hermes",
    scopes: list[str] | None = None,
) -> dict[str, Any]:
    response = client.post(
        "/api/agent-tokens",
        json={"name": name, "scopes": scopes or ["read", "write"]},
        headers=ORIGIN,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _site(client: TestClient, url: str, *, name: str | None = None) -> dict[str, Any]:
    response = client.post(
        "/api/library/sites",
        json={"name": name or url[:80], "url": url},
        headers=ORIGIN,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _rpc(
    client: TestClient,
    token: str | None,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    request_id: int | None = 1,
    extra_headers: dict[str, str] | None = None,
):
    headers = {"Accept": MCP_ACCEPT, **(extra_headers or {})}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        body["params"] = params
    if request_id is not None:
        body["id"] = request_id
    return client.post("/api/mcp", json=body, headers=headers)


def _call(
    client: TestClient,
    token: str,
    name: str,
    arguments: dict[str, Any] | None = None,
) -> tuple[Any, bool]:
    # The MCP route must never fall back to the website session cookie.
    saved = dict(client.cookies)
    client.cookies.clear()
    try:
        response = _rpc(client, token, "tools/call", {"name": name, "arguments": arguments or {}})
    finally:
        for key, value in saved.items():
            client.cookies.set(key, value)
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    text = result["content"][0]["text"]
    if result.get("isError"):
        return text, True
    return json.loads(text), False


def _ok(client: TestClient, token: str, name: str, arguments: dict[str, Any] | None = None):
    payload, is_error = _call(client, token, name, arguments)
    assert not is_error, payload
    return payload


def _error(client: TestClient, token: str, name: str, arguments: dict[str, Any] | None = None):
    payload, is_error = _call(client, token, name, arguments)
    assert is_error, payload
    return str(payload)


def _query(database_path: Path, sql: str, *params: object) -> list[tuple[Any, ...]]:
    with sqlite3.connect(database_path) as connection:
        return connection.execute(sql, params).fetchall()


def _execute(database_path: Path, sql: str, *params: object) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute(sql, params)
        connection.commit()


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")


def _audit_rows(database_path: Path, user_id: str) -> list[tuple[Any, ...]]:
    return _query(
        database_path,
        "SELECT tool, site_id, summary FROM agent_audit_log WHERE user_id = ? "
        "ORDER BY created_at, id",
        user_id,
    )


# ------------------------------------------------------------- website REST


def test_token_plaintext_is_returned_once_and_only_its_hash_is_stored(
    environment: Environment,
) -> None:
    client = environment.client
    user_id, _ = _register(client, "token-owner")
    created = _create_token(client, name="  Hermes   主力 ")
    plaintext = created["token"]
    assert plaintext.startswith("whk_")
    assert len(plaintext) == len("whk_") + 43
    assert created["name"] == "Hermes 主力"
    assert created["prefix"] == plaintext[:8]
    assert created["scopes"] == ["read", "write"]
    assert created["last_used_at"] is None

    rows = _query(
        environment.database_path,
        "SELECT name, token_hash, token_prefix, scopes, revoked_at FROM agent_tokens "
        "WHERE user_id = ?",
        user_id,
    )
    assert rows == [
        (
            "Hermes 主力",
            hashlib.sha256(plaintext.encode()).hexdigest(),
            plaintext[:8],
            "read write",
            None,
        )
    ]
    with sqlite3.connect(environment.database_path) as connection:
        dump = "\n".join(connection.iterdump())
    assert plaintext not in dump

    listed = client.get("/api/agent-tokens")
    assert listed.status_code == 200
    body = listed.json()
    assert body["max_active"] == token_service.MAX_ACTIVE_TOKENS
    assert [set(item) for item in body["items"]] == [
        {"id", "name", "prefix", "scopes", "created_at", "last_used_at"}
    ]
    assert plaintext not in listed.text
    assert rows[0][1] not in listed.text

    read_only = _create_token(client, name="grokbot", scopes=["read"])
    assert read_only["scopes"] == ["read"]
    # Write implies read, whatever the client sends.
    implied = _create_token(client, name="writer", scopes=["write"])
    assert implied["scopes"] == ["read", "write"]
    names = [item["name"] for item in client.get("/api/agent-tokens").json()["items"]]
    assert sorted(names) == sorted(["Hermes 主力", "grokbot", "writer"])


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "x", "scopes": ["read"], "extra": 1},
        {"name": "", "scopes": ["read"]},
        {"name": "x" * 41, "scopes": ["read"]},
        {"name": "   ", "scopes": ["read"]},
        {"name": "x", "scopes": []},
        {"name": "x", "scopes": ["admin"]},
        {"name": "x"},
    ],
)
def test_token_create_request_is_strict(environment: Environment, payload: dict) -> None:
    client = environment.client
    _register(client, "strict-owner")
    response = client.post("/api/agent-tokens", json=payload, headers=ORIGIN)
    assert response.status_code == 422, response.text


def test_token_routes_need_session_and_trusted_origin(environment: Environment) -> None:
    client = environment.client
    assert client.get("/api/agent-tokens").status_code == 401
    assert client.get("/api/agent-activity").status_code == 401
    _register(client, "origin-owner")
    no_origin = client.post("/api/agent-tokens", json={"name": "x", "scopes": ["read"]})
    assert no_origin.status_code == 403
    foreign = client.post(
        "/api/agent-tokens",
        json={"name": "x", "scopes": ["read"]},
        headers={"Origin": "https://evil.example"},
    )
    assert foreign.status_code == 403
    token = _create_token(client)
    assert client.delete(f"/api/agent-tokens/{token['id']}").status_code == 403


def test_active_token_limit_and_revoke(environment: Environment) -> None:
    client = environment.client
    _register(client, "limit-owner")
    created = [_create_token(client, name=f"agent {index}") for index in range(20)]
    over = client.post(
        "/api/agent-tokens",
        json={"name": "one more", "scopes": ["read"]},
        headers=ORIGIN,
    )
    assert over.status_code == 409
    assert over.json()["detail"]["code"] == "token_limit"

    revoked = client.delete(f"/api/agent-tokens/{created[0]['id']}", headers=ORIGIN)
    assert revoked.status_code == 204
    # Repeating a revoke is harmless.
    assert client.delete(f"/api/agent-tokens/{created[0]['id']}", headers=ORIGIN).status_code == 204
    assert client.delete("/api/agent-tokens/missing", headers=ORIGIN).status_code == 404
    listed = client.get("/api/agent-tokens").json()["items"]
    assert len(listed) == 19
    assert created[0]["id"] not in {item["id"] for item in listed}
    _create_token(client, name="replacement")


# ------------------------------------------------------------------ MCP auth


def test_mcp_round_trip_through_the_real_route(environment: Environment) -> None:
    client = environment.client
    _register(client, "round-trip")
    site = _site(client, "https://github.com/langchain-ai/langgraph", name="LangGraph")
    token = _create_token(client)["token"]
    client.cookies.clear()

    initialized = _rpc(
        client,
        token,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "1"},
        },
    )
    assert initialized.status_code == 200, initialized.text
    assert initialized.headers["content-type"].startswith("application/json")
    result = initialized.json()["result"]
    assert result["serverInfo"]["name"] == "webhub"
    assert "personal library" in result["instructions"]
    assert "tools" in result["capabilities"]

    notified = _rpc(client, token, "notifications/initialized", request_id=None)
    assert notified.status_code == 202

    listed = _rpc(client, token, "tools/list", {}, request_id=2)
    assert listed.status_code == 200
    tools = {tool["name"]: tool for tool in listed.json()["result"]["tools"]}
    assert set(tools) == TOOL_NAMES
    assert tools["search_resources"]["annotations"]["readOnlyHint"] is True
    assert tools["save_resource"]["annotations"]["destructiveHint"] is False
    assert tools["search_resources"]["inputSchema"]["properties"]["limit"]["maximum"] == 30
    assert all(len(tool["description"]) > 80 for tool in tools.values())

    called = _rpc(
        client,
        token,
        "tools/call",
        {"name": "search_resources", "arguments": {"query": "langgraph"}},
        request_id=3,
        extra_headers={"MCP-Protocol-Version": "2025-06-18"},
    )
    assert called.status_code == 200
    payload = json.loads(called.json()["result"]["content"][0]["text"])
    assert payload["total"] == 1
    assert payload["items"] == [
        {
            "id": site["id"],
            "title": "LangGraph",
            "url": "https://github.com/langchain-ai/langgraph",
            "kind": "repo",
            "summary": "",
            "category": "未分类",
            "tags": [],
            "saved_at": payload["items"][0]["saved_at"],
            "opened": False,
            "starred": False,
        }
    ]
    assert payload["items"][0]["saved_at"].endswith("Z")
    # Exact path: no redirect to a trailing slash.
    assert _rpc(client, None, "tools/list").status_code == 401
    # Stateless: no standalone SSE stream to hold open (spec-allowed 405).
    stream = client.get(
        "/api/mcp",
        headers={"Accept": "text/event-stream", "Authorization": f"Bearer {token}"},
    )
    assert stream.status_code == 405
    assert client.get("/api/mcp", headers={"Accept": "text/event-stream"}).status_code == 401
    assert client.put("/api/mcp", headers={"Authorization": f"Bearer {token}"}).status_code == 405


def test_mcp_rejects_missing_unknown_revoked_and_inactive_tokens(
    environment: Environment,
) -> None:
    client = environment.client
    user_id, cookie = _register(client, "auth-owner")
    token = _create_token(client)
    plaintext = token["token"]

    # The session cookie alone is never enough on the MCP route.
    response = _rpc(client, None, "tools/list")
    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"
    assert response.headers["www-authenticate"].startswith("Bearer")
    client.cookies.clear()
    for header in (
        "Bearer whk_not-a-real-token",
        f"Basic {plaintext}",
        "Bearer",
        f"Bearer {plaintext[:-1]}",
        f"Bearer {plaintext.removeprefix('whk_')}",
    ):
        bad = client.post(
            "/api/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Accept": MCP_ACCEPT, "Authorization": header},
        )
        assert bad.status_code == 401, header
    assert _rpc(client, plaintext, "tools/list").status_code == 200

    _use_cookie(client, cookie)
    assert client.delete(f"/api/agent-tokens/{token['id']}", headers=ORIGIN).status_code == 204
    client.cookies.clear()
    assert _rpc(client, plaintext, "tools/list").status_code == 401

    _use_cookie(client, cookie)
    other = _create_token(client, name="second")["token"]
    client.cookies.clear()
    assert _rpc(client, other, "tools/list").status_code == 200
    _execute(environment.database_path, "UPDATE users SET is_active = 0 WHERE id = ?", user_id)
    assert _rpc(client, other, "tools/list").status_code == 401


def test_last_used_at_is_recorded_and_throttled(environment: Environment) -> None:
    client = environment.client
    _register(client, "usage-owner")
    token = _create_token(client)

    def last_used() -> str | None:
        return _query(
            environment.database_path,
            "SELECT last_used_at FROM agent_tokens WHERE id = ?",
            token["id"],
        )[0][0]

    assert last_used() is None
    _ok(client, token["token"], "list_taxonomy")
    first = last_used()
    assert first is not None
    _ok(client, token["token"], "list_taxonomy")
    assert last_used() == first

    stale = _stamp(datetime.now(UTC) - timedelta(minutes=5))
    _execute(
        environment.database_path,
        "UPDATE agent_tokens SET last_used_at = ? WHERE id = ?",
        stale,
        token["id"],
    )
    _ok(client, token["token"], "list_taxonomy")
    refreshed = last_used()
    assert refreshed is not None and refreshed > stale
    listed = client.get("/api/agent-tokens").json()["items"]
    assert listed[0]["last_used_at"] is not None


# ------------------------------------------------------------ scopes & scope


def test_read_only_token_reads_but_cannot_write(environment: Environment) -> None:
    client = environment.client
    user_id, _ = _register(client, "reader")
    site = _site(client, "https://example.com/blog/post-1", name="A post")
    token = _create_token(client, name="grokbot", scopes=["read"])["token"]

    assert _ok(client, token, "search_resources")["total"] == 1
    assert _ok(client, token, "get_resource", {"id": site["id"]})["title"] == "A post"
    for name, arguments in (
        ("save_resource", {"url": "https://new.example/"}),
        ("update_resource", {"id": site["id"], "starred": True}),
        ("mark_resource", {"id": site["id"], "action": "opened"}),
    ):
        message = _error(client, token, name, arguments)
        assert "this token is read-only" in message
    assert _query(environment.database_path, "SELECT count(*) FROM sites")[0][0] == 1
    detail = client.get(f"/api/library/sites/{site['id']}").json()
    assert detail["starred"] is False and detail["open_count"] == 0
    assert _audit_rows(environment.database_path, user_id) == []


def test_tokens_are_confined_to_their_own_account(environment: Environment) -> None:
    client = environment.client
    owner_id, owner_cookie = _register(client, "owner-a")
    secret = _site(client, "https://secret.example/notes/plan", name="A 的秘密")
    owner_token = _create_token(client, name="A agent")
    _ok(client, owner_token["token"], "mark_resource", {"id": secret["id"], "action": "seen"})

    intruder_id, intruder_cookie = _register(client, "owner-b")
    _use_cookie(client, intruder_cookie)
    intruder = _create_token(client, name="B agent")["token"]

    foreign_search = _ok(client, intruder, "search_resources", {"query": "秘密"})
    assert foreign_search["total"] == 0 and foreign_search["items"] == []
    assert _ok(client, intruder, "search_resources")["total"] == 0
    for name, arguments in (
        ("get_resource", {"id": secret["id"]}),
        ("update_resource", {"id": secret["id"], "add_tags": ["x"], "starred": True}),
        ("mark_resource", {"id": secret["id"], "action": "opened"}),
    ):
        assert "Not found" in _error(client, intruder, name, arguments)
    # B saving the same URL creates B's own item, not a reference to A's.
    saved = _ok(client, intruder, "save_resource", {"url": "https://secret.example/notes/plan"})
    assert saved["already_saved"] is False
    assert saved["resource"]["id"] != secret["id"]

    foreign_revoke = client.delete(f"/api/agent-tokens/{owner_token['id']}", headers=ORIGIN)
    assert foreign_revoke.status_code == 404
    assert [item["tool"] for item in client.get("/api/agent-activity").json()["items"]] == [
        "save_resource"
    ]
    assert len(_audit_rows(environment.database_path, intruder_id)) == 1

    _use_cookie(client, owner_cookie)
    detail = client.get(f"/api/library/sites/{secret['id']}").json()
    assert detail["open_count"] == 0 and detail["starred"] is False and detail["tags"] == []
    assert [item["tool"] for item in client.get("/api/agent-activity").json()["items"]] == [
        "mark_resource"
    ]
    assert len(_audit_rows(environment.database_path, owner_id)) == 1


# ------------------------------------------------------------------- tools


def test_save_resource_creates_dedupes_and_reuses_tags(environment: Environment) -> None:
    client = environment.client
    user_id, _ = _register(client, "saver")
    existing_tag = client.post("/api/library/tags", json={"name": "Agent"}, headers=ORIGIN).json()
    token = _create_token(client)["token"]

    saved = _ok(
        client,
        token,
        "save_resource",
        {
            "url": "https://www.example.com/blog/agents?utm_source=x",
            "tags": ["agent", " 新  标签 ", "AGENT"],
            "note": "值得细读的 Agent 设计文章",
        },
    )
    assert saved["already_saved"] is False
    resource = saved["resource"]
    assert resource["title"] == "example.com"
    assert resource["source"] == "agent"
    assert resource["kind"] == "article"
    assert resource["description"] == "值得细读的 Agent 设计文章"
    assert resource["tags"] == ["Agent", "新 标签"]
    tags = {item["name"]: item for item in client.get("/api/library/tags").json()["items"]}
    assert set(tags) == {"Agent", "新 标签"}
    assert tags["Agent"]["id"] == existing_tag["id"]
    assert environment.scheduled == [(user_id, (resource["id"],))]

    again = _ok(
        client,
        token,
        "save_resource",
        # Same identity after normalization (scheme/host case, default port).
        {
            "url": "HTTPS://WWW.Example.com:443/blog/agents?utm_source=x",
            "title": "Other",
            "tags": ["新的"],
        },
    )
    assert again["already_saved"] is True
    assert again["resource"]["id"] == resource["id"]
    assert again["resource"]["title"] == "example.com"
    assert "新的" not in {item["name"] for item in client.get("/api/library/tags").json()["items"]}
    assert len(environment.scheduled) == 1

    repo = _ok(client, token, "save_resource", {"url": "https://github.com/acme/widgets"})
    assert repo["resource"]["title"] == "acme/widgets"
    titled = _ok(
        client,
        token,
        "save_resource",
        {"url": "https://github.com/owner/repo", "title": "  Owner   Repo "},
    )
    assert titled["resource"]["title"] == "Owner Repo"
    assert titled["resource"]["kind"] == "repo"
    assert titled["resource"]["meta"] == {"host": "github.com", "owner": "owner", "repo": "repo"}

    assert "Cannot save this URL" in _error(client, token, "save_resource", {"url": "ftp://x"})
    too_many = [f"t{index}" for index in range(9)]
    assert _error(client, token, "save_resource", {"url": "https://a.example/", "tags": too_many})
    assert "Invalid tag name" in _error(
        client,
        token,
        "save_resource",
        {"url": "https://b.example/", "tags": ["https://evil.example/x"]},
    )
    assert _query(environment.database_path, "SELECT count(*) FROM sites")[0][0] == 3
    assert [row[0] for row in _audit_rows(environment.database_path, user_id)] == [
        "save_resource",
        "save_resource",
        "save_resource",
    ]
    assert _audit_rows(environment.database_path, user_id)[0][2] == (
        "收藏 https://www.example.com/blog/agents?utm_source=x"
    )


def test_update_resource_changes_only_given_fields(environment: Environment) -> None:
    client = environment.client
    user_id, _ = _register(client, "updater")
    site = _site(client, "https://tool.example/", name="Tool")
    client.post("/api/library/tags", json={"name": "Old"}, headers=ORIGIN)
    category = client.post(
        "/api/library/categories", json={"name": "工具箱"}, headers=ORIGIN
    ).json()
    token = _create_token(client)["token"]

    message = _error(client, token, "update_resource", {"id": site["id"], "category": "不存在"})
    assert "No category named" in message
    assert "工具箱" in message and "未分类" in message
    assert "Nothing to update" in _error(client, token, "update_resource", {"id": site["id"]})

    changed = _ok(
        client,
        token,
        "update_resource",
        {
            "id": site["id"],
            "add_tags": ["old", "Fresh"],
            "category": " 工具箱 ",
            "starred": True,
            "kind": "repo",
        },
    )
    assert changed["changed"] is True
    resource = changed["resource"]
    assert resource["tags"] == ["Fresh", "Old"]
    assert resource["category"] == "工具箱"
    assert resource["starred"] is True
    assert resource["kind"] == "repo"
    detail = client.get(f"/api/library/sites/{site['id']}").json()
    assert detail["category"]["id"] == category["id"]
    assert detail["kind_source"] == "manual"

    removed = _ok(
        client,
        token,
        "update_resource",
        {"id": site["id"], "remove_tags": ["OLD", "never-there"], "starred": False},
    )
    assert removed["resource"]["tags"] == ["Fresh"]
    assert removed["resource"]["starred"] is False
    assert removed["not_tagged"] == ["never-there"]

    unchanged = _ok(
        client,
        token,
        "update_resource",
        {"id": site["id"], "add_tags": ["fresh"], "category": "工具箱", "kind": "repo"},
    )
    assert unchanged["changed"] is False
    summaries = [row[2] for row in _audit_rows(environment.database_path, user_id)]
    assert summaries == [
        "Tool：添加标签 Old、Fresh；分类改为 工具箱；类型改为仓库；加星标",
        "Tool：移除标签 Old；取消星标",
    ]


def test_update_resource_retries_after_a_version_conflict(
    environment: Environment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = environment.client
    _register(client, "conflict")
    site = _site(client, "https://conflict.example/", name="Conflict")
    token = _create_token(client)["token"]
    original = library_service.update_site
    calls: list[int] = []

    async def flaky_update(session, user_id, site_id, payload):
        calls.append(payload.expected_version)
        if len(calls) == 1:
            raise library_service.LibraryConflictError("changed", code="version_conflict")
        return await original(session, user_id, site_id, payload)

    monkeypatch.setattr(agent_tools.service, "update_site", flaky_update)
    result = _ok(client, token, "update_resource", {"id": site["id"], "add_tags": ["Retry"]})
    assert result["resource"]["tags"] == ["Retry"]
    assert len(calls) == 2
    assert [item["name"] for item in client.get("/api/library/tags").json()["items"]] == ["Retry"]


def test_mark_resource_applies_usage_signals(environment: Environment) -> None:
    client = environment.client
    user_id, _ = _register(client, "marker")
    site = _site(client, "https://watch.example/video/1", name="Talk")
    token = _create_token(client)["token"]
    before = client.get(f"/api/library/sites/{site['id']}").json()

    opened = _ok(client, token, "mark_resource", {"id": site["id"], "action": "opened"})
    assert opened["resource"]["open_count"] == 1
    assert opened["resource"]["opened"] is True
    assert opened["resource"]["last_opened_at"] is not None
    _ok(client, token, "mark_resource", {"id": site["id"], "action": "seen"})
    _ok(client, token, "mark_resource", {"id": site["id"], "action": "snooze", "snooze_days": 14})
    muted = _ok(client, token, "mark_resource", {"id": site["id"], "action": "mute"})
    assert muted["resource"]["resurface_muted"] is True
    assert muted["resource"]["seen"] is True

    due, count = _query(
        environment.database_path,
        "SELECT resurface_due_at, resurface_count FROM sites WHERE id = ?",
        site["id"],
    )[0]
    assert count == 1
    assert due > _stamp(datetime.now(UTC) + timedelta(days=13))
    after = client.get(f"/api/library/sites/{site['id']}").json()
    # Usage is not editing: version and updated_at stay put.
    assert (after["version"], after["updated_at"]) == (before["version"], before["updated_at"])

    assert _error(
        client, token, "mark_resource", {"id": site["id"], "action": "snooze", "snooze_days": 91}
    )
    assert _error(client, token, "mark_resource", {"id": site["id"], "action": "delete"})
    assert [row[2] for row in _audit_rows(environment.database_path, user_id)] == [
        "标记已打开 Talk",
        "标记已看 Talk",
        "推迟 14 天再重温 Talk",
        "不再重温 Talk",
    ]


def test_read_tools_and_activity_feed(environment: Environment) -> None:
    client = environment.client
    user_id, _ = _register(client, "browser")
    tag = client.post("/api/library/tags", json={"name": "Agent"}, headers=ORIGIN).json()
    old = client.post(
        "/api/library/sites",
        json={"name": "Old repo", "url": "https://github.com/a/b", "tag_ids": [tag["id"]]},
        headers=ORIGIN,
    ).json()
    fresh = _site(client, "https://news.example/", name="News")
    _execute(
        environment.database_path,
        "UPDATE sites SET created_at = ? WHERE id = ?",
        _stamp(datetime.now(UTC) - timedelta(days=20)),
        old["id"],
    )
    token = _create_token(client, name="Hermes")["token"]

    by_kind = _ok(client, token, "search_resources", {"kind": "repo"})
    assert [item["id"] for item in by_kind["items"]] == [old["id"]]
    by_tag = _ok(client, token, "search_resources", {"tag": "agent"})
    assert [item["id"] for item in by_tag["items"]] == [old["id"]]
    assert by_tag["items"][0]["tags"] == ["Agent"]
    missing_tag = _ok(client, token, "search_resources", {"tag": "nope"})
    assert missing_tag["total"] == 0 and "list_taxonomy" in missing_tag["note"]
    unopened = _ok(client, token, "search_resources", {"state": "unopened", "limit": 1})
    assert unopened["total"] == 2 and len(unopened["items"]) == 1
    assert _error(client, token, "search_resources", {"limit": 31})
    assert _error(client, token, "search_resources", {"kind": "podcast"})

    detail = _ok(client, token, "get_resource", {"id": fresh["id"]})
    assert {"description", "meta", "open_count", "last_opened_at"} <= set(detail)

    rediscover = _ok(client, token, "get_rediscover", {"limit": 4})
    assert [item["id"] for item in rediscover["items"]] == [old["id"]]
    assert rediscover["items"][0]["saved_days"] == 20
    assert "20 天前收藏" in rediscover["items"][0]["reason"]

    taxonomy = _ok(client, token, "list_taxonomy")
    assert taxonomy["categories"] == [{"name": "未分类", "count": 2, "is_default": True}]
    assert taxonomy["tags"] == [{"name": "Agent", "count": 1}]
    # Reads leave no audit trail.
    assert _audit_rows(environment.database_path, user_id) == []

    _ok(client, token, "mark_resource", {"id": fresh["id"], "action": "opened"})
    _ok(client, token, "update_resource", {"id": fresh["id"], "starred": True})
    activity = client.get("/api/agent-activity").json()["items"]
    assert [(item["tool"], item["token_name"], item["site_id"]) for item in activity] == [
        ("update_resource", "Hermes", fresh["id"]),
        ("mark_resource", "Hermes", fresh["id"]),
    ]
    assert activity[0]["summary"] == "News：加星标"
    assert len(client.get("/api/agent-activity", params={"limit": 1}).json()["items"]) == 1
    assert client.get("/api/agent-activity", params={"limit": 0}).status_code == 422


def test_search_guides_agents_past_all_keyword_misses(environment: Environment) -> None:
    client = environment.client
    _register(client, "keyword-searcher")
    site = _site(client, "https://github.com/langchain-ai/langgraph", name="LangGraph")
    token = _create_token(client)["token"]

    # Keyword search requires every keyword. A sentence-like query from an
    # LLM finds nothing, so the result must tell the Agent how to retry
    # instead of reading as "nothing saved" and falling back to the web.
    sentence = _ok(
        client, token, "search_resources", {"query": "langgraph agent framework I saved"}
    )
    assert sentence["total"] == 0
    assert "every keyword" in sentence["note"]
    retried = _ok(client, token, "search_resources", {"query": "langgraph"})
    assert [item["id"] for item in retried["items"]] == [site["id"]]
    assert "note" not in retried
    # Browsing an empty library is not a keyword miss.
    browse = _ok(client, token, "search_resources", {"kind": "video"})
    assert browse["total"] == 0 and "note" not in browse

    listed = _rpc(client, token, "tools/list", {}).json()["result"]["tools"]
    search = next(tool for tool in listed if tool["name"] == "search_resources")
    assert "every keyword" in search["description"]
    # The keyword index does not cover the short summary; do not promise it.
    assert "summary," not in search["inputSchema"]["properties"]["query"]["description"]


def test_save_resource_shortens_long_titles(environment: Environment) -> None:
    client = environment.client
    _register(client, "long-titles")
    token = _create_token(client)["token"]

    title = "很长的文章标题" * 40
    saved = _ok(
        client, token, "save_resource", {"url": "https://blog.example/long", "title": title}
    )
    assert saved["resource"]["title"] == title[:160]


def test_resolve_token_ignores_malformed_values(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'resolve.sqlite3').as_posix()}"
    upgrade_database(database_url)

    async def scenario() -> list[object]:
        database = Database(database_url)
        try:
            async with database.sessions() as session:
                return [
                    await token_service.resolve_token(session, raw)
                    for raw in ("", "whk_", "whk_ space", "x" * 50, "whk_" + "a" * 200)
                ]
        finally:
            await database.dispose()

    assert asyncio.run(scenario()) == [None] * 5


def test_migration_creates_and_drops_agent_tables(tmp_path: Path) -> None:
    database_path = tmp_path / "agent-tokens.sqlite3"
    config = create_alembic_config(f"sqlite+aiosqlite:///{database_path.as_posix()}")
    command.upgrade(config, "20261021_0020")

    def tables() -> set[str]:
        return {
            row[0]
            for row in _query(database_path, "SELECT name FROM sqlite_master WHERE type='table'")
        }

    assert not {"agent_tokens", "agent_audit_log"} & tables()
    command.upgrade(config, "head")
    command.check(config)
    assert {"agent_tokens", "agent_audit_log"} <= tables()
    indexes = {
        row[0] for row in _query(database_path, "SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert {"ix_agent_tokens_user_revoked_created", "ix_agent_audit_log_user_created_id"} <= indexes
    command.downgrade(config, "20261021_0020")
    assert not {"agent_tokens", "agent_audit_log"} & tables()
