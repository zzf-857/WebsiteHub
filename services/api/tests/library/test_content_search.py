"""Q48: content snapshots (page text / README), content search and MCP fields."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from alembic import command
from webhub.config import Settings
from webhub.db.database import Database
from webhub.db.migrations import create_alembic_config, upgrade_database
from webhub.ingestion import service as ingestion_service
from webhub.ingestion.content import (
    MAX_CONTENT_CHARS,
    choose_snapshot,
    clean_content,
    store_content_snapshot,
)
from webhub.ingestion.fetcher import FetchOutcome, SiteMetadata, fetch_site_metadata
from webhub.ingestion.github import GitHubCredential, GitHubFetchResult, GitHubRepoRef
from webhub.ingestion.worker import AnalysisSchedule
from webhub.library.service.listing import content_snippet
from webhub.main import create_app

ORIGIN = {"Origin": "http://testserver"}
MCP_ACCEPT = "application/json, text/event-stream"
PREVIOUS = "20261022_0022"


@dataclass
class Env:
    client: TestClient
    path: Path
    url: str
    pages: dict[str, str] = field(default_factory=dict)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    path = tmp_path / "main.sqlite3"
    url = f"sqlite+aiosqlite:///{path.as_posix()}"
    settings = Settings(environment="test", database_url=url, data_directory=tmp_path)
    upgrade_database(url)
    pages: dict[str, str] = {}
    monkeypatch.setattr(
        "webhub.ingestion.worker.schedule_analysis",
        lambda *_args, **_kwargs: AnalysisSchedule(queued=0, already_queued=0, rejected=0),
    )
    monkeypatch.setattr(
        "webhub.ingestion.worker.ensure_auto_backfill", lambda *_args, **_kwargs: False
    )

    async def fetched(target: str, **_kwargs: object) -> FetchOutcome:
        return FetchOutcome(
            status="complete",
            reason="",
            metadata=SiteMetadata(title="Page", page_text=pages.get(target, "")),
            final_url=target,
        )

    monkeypatch.setattr(ingestion_service, "fetch_site_metadata", fetched)
    with TestClient(create_app(settings=settings)) as client:
        registered = client.post(
            "/api/auth/register",
            json={"username": "alice", "password": "a sufficiently secure password"},
            headers=ORIGIN,
        )
        assert registered.status_code == 201
        yield Env(client, path, url, pages)


def _site(env: Env, url: str, name: str) -> dict[str, Any]:
    response = env.client.post(
        "/api/library/sites", json={"name": name, "url": url}, headers=ORIGIN
    )
    assert response.status_code == 201, response.text
    return response.json()


def _query(env: Env, sql: str, *params: object) -> list[tuple[Any, ...]]:
    with sqlite3.connect(env.path) as connection:
        return connection.execute(sql, params).fetchall()


def _user_id(env: Env, site_id: str) -> str:
    return str(_query(env, "SELECT user_id FROM sites WHERE id = ?", site_id)[0][0])


def _run(env: Env, coroutine_factory: Any) -> Any:
    async def scenario() -> Any:
        database = Database(env.url)
        try:
            async with database.sessions() as session:
                return await coroutine_factory(session)
        finally:
            await database.dispose()

    return asyncio.run(scenario())


def _analyze(env: Env, site_id: str, **kwargs: Any) -> FetchOutcome | None:
    user_id = _user_id(env, site_id)
    return _run(
        env,
        lambda session: ingestion_service.analyze_site(session, user_id, site_id, **kwargs),
    )


def _store(env: Env, site_id: str, url: str, content: str, source: str = "page") -> bool:
    user_id = _user_id(env, site_id)
    return _run(
        env,
        lambda session: store_content_snapshot(
            session,
            user_id=user_id,
            site_id=site_id,
            url=url,
            content=content,
            source=source,  # type: ignore[arg-type]
        ),
    )


def _snapshot(env: Env, site_id: str) -> tuple[Any, ...] | None:
    rows = _query(
        env,
        "SELECT content, char_count, source FROM site_contents WHERE site_id = ?",
        site_id,
    )
    return rows[0] if rows else None


def _fts_rows(env: Env, site_id: str) -> list[tuple[Any, ...]]:
    return _query(
        env, "SELECT user_id, content FROM site_content_search WHERE site_id = ?", site_id
    )


# ---------------------------------------------------------------- migration


def test_migration_creates_and_drops_the_snapshot_tables(tmp_path: Path) -> None:
    path = tmp_path / "migrate.sqlite3"
    config = create_alembic_config(f"sqlite+aiosqlite:///{path.as_posix()}")

    def names(kind: str) -> set[str]:
        with sqlite3.connect(path) as connection:
            return {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = ?", (kind,)
                )
            }

    command.upgrade(config, PREVIOUS)
    assert not {"site_contents", "site_content_search"} & names("table")
    command.upgrade(config, "head")
    command.check(config)
    assert {"site_contents", "site_content_search"} <= names("table")
    assert {
        "site_contents_search_insert",
        "site_contents_search_update",
        "site_contents_search_delete",
    } <= names("trigger")
    command.downgrade(config, PREVIOUS)
    assert not {"site_contents", "site_content_search"} & names("table")
    assert not {name for name in names("trigger") if name.startswith("site_contents_")}
    command.upgrade(config, "head")
    command.check(config)


# ------------------------------------------------------------------ writing


def test_clean_and_choose_snapshot() -> None:
    assert clean_content("  a   b \n\n  c ") == "a b\nc"
    assert len(clean_content("x" * (MAX_CONTENT_CHARS + 50))) == MAX_CONTENT_CHARS
    assert choose_snapshot("page words", "") == ("page words", "page")
    assert choose_snapshot("page words", "# Readme") == ("# Readme", "readme")
    assert choose_snapshot("  ", "") is None


def test_analysis_keeps_only_the_latest_page_text(env: Env) -> None:
    site = _site(env, "https://blog.example/post", "A post")
    before = _query(env, "SELECT version, updated_at FROM sites WHERE id = ?", site["id"])
    env.pages["https://blog.example/post"] = "First   version\n\nof the body"

    assert _analyze(env, site["id"]) is not None
    assert _snapshot(env, site["id"]) == ("First version\nof the body", 25, "page")
    assert _fts_rows(env, site["id"]) == [(_user_id(env, site["id"]), "First version\nof the body")]
    # Derived data: neither the edit version nor updated_at moves.
    assert _query(env, "SELECT version, updated_at FROM sites WHERE id = ?", site["id"]) == before

    env.pages["https://blog.example/post"] = "Second body"
    _analyze(env, site["id"])
    assert _snapshot(env, site["id"]) == ("Second body", 11, "page")
    assert [row[1] for row in _fts_rows(env, site["id"])] == ["Second body"]

    # A later analysis that reads no text keeps the last snapshot.
    env.pages["https://blog.example/post"] = ""
    _analyze(env, site["id"])
    assert _snapshot(env, site["id"]) == ("Second body", 11, "page")
    assert _query(env, "SELECT COUNT(*) FROM site_contents") == [(1,)]


@dataclass
class ReadmeEnricher:
    readme: str

    async def credential(self, _session: object, _user_id: str) -> GitHubCredential | None:
        return None

    async def fetch(
        self,
        _ref: GitHubRepoRef,
        *,
        credential: GitHubCredential | None,
        etag: str | None,
        include_stats: bool,
        include_readme: bool,
    ) -> GitHubFetchResult:
        del credential, etag, include_stats
        return GitHubFetchResult(
            "skipped", readme_excerpt=self.readme if include_readme else ""
        )


def test_readme_excerpt_is_stored_for_repositories(env: Env) -> None:
    url = "https://github.com/langchain-ai/langgraph"
    site = _site(env, url, "LangGraph")
    env.pages[url] = "GitHub navigation chrome"

    class FailingModel:
        async def enrich(self, _request: object) -> object:
            from webhub.ingestion.enrichment import SiteEnrichmentUnavailableError

            raise SiteEnrichmentUnavailableError("模型未返回可用结果")

    _analyze(
        env,
        site["id"],
        use_llm=True,
        enricher=FailingModel(),
        repository_enricher=ReadmeEnricher("# LangGraph\nLow-level orchestration."),
    )
    assert _snapshot(env, site["id"]) == ("# LangGraph\nLow-level orchestration.", 36, "readme")


def test_a_url_change_discards_the_snapshot(env: Env) -> None:
    site = _site(env, "https://blog.example/a", "A")
    assert _store(env, site["id"], "https://blog.example/other", "stale text") is False
    assert _snapshot(env, site["id"]) is None
    assert _store(env, site["id"], "https://blog.example/a", "fresh text") is True
    assert _snapshot(env, site["id"]) == ("fresh text", 10, "page")


def test_private_urls_are_never_fetched_so_no_snapshot_appears(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Use the real fetcher: it refuses loopback / private targets before any I/O.
    monkeypatch.setattr(ingestion_service, "fetch_site_metadata", fetch_site_metadata)
    site = _site(env, "http://127.0.0.1:9/secret", "Router admin")
    _analyze(env, site["id"])
    assert _snapshot(env, site["id"]) is None
    assert _fts_rows(env, site["id"]) == []


def test_deleting_the_site_cascades_to_the_snapshot_and_index(env: Env) -> None:
    site = _site(env, "https://blog.example/gone", "Gone")
    _store(env, site["id"], "https://blog.example/gone", "orchestration notes")
    assert _fts_rows(env, site["id"])

    deleted = env.client.delete(
        f"/api/library/sites/{site['id']}",
        params={"expected_version": site["version"]},
        headers=ORIGIN,
    )
    assert deleted.status_code in {200, 204}, deleted.text
    assert _snapshot(env, site["id"]) is None
    assert _fts_rows(env, site["id"]) == []


# ------------------------------------------------------------------ search


def _seed_search(env: Env) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    titled = _site(env, "https://titled.example/", "Orchestration handbook")
    body = _site(env, "https://body.example/", "Weekly notes")
    other = _site(env, "https://other.example/", "Unrelated")
    _store(
        env,
        body["id"],
        "https://body.example/",
        "Intro paragraph. " * 10
        + "Durable orchestration for long-running agents. "
        + "Tail. " * 20,
    )
    _store(env, other["id"], "https://other.example/", "Nothing relevant here")
    # The body hit is the most recently updated, so plain recency would rank it first.
    with sqlite3.connect(env.path) as connection:
        connection.execute(
            "UPDATE sites SET updated_at = '2030-01-01 00:00:00.000000' WHERE id = ?",
            (body["id"],),
        )
        connection.commit()
    return titled, body, other


def test_library_search_matches_content_and_ranks_title_hits_first(env: Env) -> None:
    titled, body, _ = _seed_search(env)

    relevance = env.client.get(
        "/api/library/sites", params={"q": "orchestration", "sort": "relevance"}
    )
    assert relevance.status_code == 200, relevance.text
    payload = relevance.json()
    assert [item["id"] for item in payload["items"]] == [titled["id"], body["id"]]
    assert payload["aggregate"]["matched_count"] == 2
    assert set(payload["content_matches"]) == {body["id"]}
    snippet = payload["content_matches"][body["id"]]
    assert "Durable orchestration for long-running agents." in snippet
    assert snippet.startswith("…") and snippet.endswith("…")
    assert len(snippet) <= 60 * 2 + len("orchestration") + 2

    # Other sorts keep their order but still mark the content hit.
    recent = env.client.get("/api/library/sites", params={"q": "orchestration"}).json()
    assert [item["id"] for item in recent["items"]] == [body["id"], titled["id"]]
    assert set(recent["content_matches"]) == {body["id"]}

    # Every keyword must appear somewhere: title + content may share the work.
    mixed = env.client.get("/api/library/sites", params={"q": "weekly durable"}).json()
    assert [item["id"] for item in mixed["items"]] == [body["id"]]

    assert env.client.get("/api/library/sites").json()["content_matches"] == {}


def test_cjk_and_short_terms_match_content_by_substring(env: Env) -> None:
    site = _site(env, "https://cn.example/", "某篇文章")
    _store(env, site["id"], "https://cn.example/", "这是一篇关于长时间运行的智能体编排的文章。")
    found = env.client.get("/api/library/sites", params={"q": "智能体"}).json()
    assert [item["id"] for item in found["items"]] == [site["id"]]
    assert "智能体" in found["content_matches"][site["id"]]


def test_content_snippet_windows() -> None:
    assert content_snippet("short text", "short") == "short text"
    text = "a " * 100 + "needle" + " b" * 100
    snippet = content_snippet(text, "NEEDLE")
    assert "needle" in snippet and snippet.startswith("…") and snippet.endswith("…")
    # A term found only through the FTS tokenizer still yields a leading excerpt.
    assert content_snippet("x" * 300, "zzz").endswith("…")


def test_content_search_is_confined_to_the_account(env: Env) -> None:
    site = _site(env, "https://private.example/", "Private")
    _store(env, site["id"], "https://private.example/", "confidential orchestration plan")
    env.client.cookies.clear()
    env.client.post(
        "/api/auth/register",
        json={"username": "mallory", "password": "a sufficiently secure password"},
        headers=ORIGIN,
    )
    foreign = env.client.get("/api/library/sites", params={"q": "confidential"}).json()
    assert foreign["items"] == [] and foreign["content_matches"] == {}


# --------------------------------------------------------------------- MCP


def _mcp(env: Env, token: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    saved = dict(env.client.cookies)
    env.client.cookies.clear()
    try:
        response = env.client.post(
            "/api/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            headers={"Accept": MCP_ACCEPT, "Authorization": f"Bearer {token}"},
        )
    finally:
        for key, value in saved.items():
            env.client.cookies.set(key, value)
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


def test_mcp_reports_where_items_matched_and_returns_content_on_request(env: Env) -> None:
    titled, body, other = _seed_search(env)
    token = env.client.post(
        "/api/agent-tokens", json={"name": "Hermes", "scopes": ["read"]}, headers=ORIGIN
    ).json()["token"]

    found = _mcp(env, token, "search_resources", {"query": "orchestration"})
    assert found["total"] == 2
    first, second = found["items"]
    assert (first["id"], first["matched_in"]) == (titled["id"], "title")
    assert "snippet" not in first
    assert (second["id"], second["matched_in"]) == (body["id"], "content")
    assert "Durable orchestration" in second["snippet"]
    # Browsing without a query carries no match fields.
    browse = _mcp(env, token, "search_resources", {})
    assert all("matched_in" not in item for item in browse["items"])

    plain = _mcp(env, token, "get_resource", {"id": body["id"]})
    assert "content" not in plain
    full = _mcp(env, token, "get_resource", {"id": body["id"], "include_content": True})
    assert full["content"]["source"] == "page"
    assert "Durable orchestration for long-running agents." in full["content"]["text"]
    assert full["content"]["chars"] == len(full["content"]["text"])
    assert full["content"]["extracted_at"].endswith("Z")
    none = _mcp(env, token, "get_resource", {"id": titled["id"], "include_content": True})
    assert none["content"] is None
    assert other["id"]
