from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from webhub.agent.tools import SOURCE_LIBRARY, AgentToolContext, build_tools
from webhub.config import Settings
from webhub.db.database import Database
from webhub.db.migrations import upgrade_database
from webhub.db.models import Site, User
from webhub.main import create_app

ORIGIN = {"Origin": "http://testserver"}
MASTER_KEY = b"provider-test-master-key-32bytes"


@contextmanager
def _two_accounts(tmp_path: Path) -> Iterator[Settings]:
    """Alice owns one site; Bob owns none.  Isolation must hold between them."""

    database_path = tmp_path / "main.sqlite3"
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        data_directory=tmp_path,
        provider_master_key=MASTER_KEY,
    )
    upgrade_database(settings.database_url)
    with TestClient(create_app(settings=settings)) as client:
        assert (
            client.post(
                "/api/auth/register",
                json={"username": "alice", "password": "a sufficiently secure password"},
                headers=ORIGIN,
            ).status_code
            == 201
        )
        created = client.post(
            "/api/library/sites",
            json={
                "name": "Qdrant 向量数据库",
                "url": "https://qdrant.tech",
                "description": "开源向量检索引擎",
            },
            headers=ORIGIN,
        )
        assert created.status_code == 201, created.text
        client.cookies.clear()
        assert (
            client.post(
                "/api/auth/register",
                json={"username": "bob", "password": "another secure password here"},
                headers=ORIGIN,
            ).status_code
            == 201
        )
    yield settings


def _tool_map(context: AgentToolContext) -> dict[str, Any]:
    return {tool.name: tool for tool in build_tools(context)}


def _invoke(settings: Settings, username: str, tool_name: str, **kwargs: Any) -> Any:
    async def scenario() -> Any:
        database = Database(settings.database_url)
        try:
            async with database.sessions() as session:
                user_id = await session.scalar(
                    select(User.id).where(User.username == username)
                )
            assert user_id is not None
            context = AgentToolContext(
                database=database,
                settings=settings,
                user_id=user_id,
            )
            return await _tool_map(context)[tool_name].ainvoke(kwargs)
        finally:
            await database.dispose()

    return asyncio.run(scenario())


def _site_count(settings: Settings) -> int:
    async def scenario() -> int:
        database = Database(settings.database_url)
        try:
            async with database.sessions() as session:
                return int(await session.scalar(select(func.count(Site.id))) or 0)
        finally:
            await database.dispose()

    return asyncio.run(scenario())


def test_search_library_returns_only_the_calling_accounts_sites(tmp_path: Path) -> None:
    with _two_accounts(tmp_path) as settings:
        alice = _invoke(settings, "alice", "search_library", query="向量")
        bob = _invoke(settings, "bob", "search_library", query="向量")

    assert alice["source"] == SOURCE_LIBRARY
    assert [item["name"] for item in alice["items"]] == ["Qdrant 向量数据库"]
    # Bob shares the database but must never see Alice's row.
    assert bob["items"] == []
    assert bob["matched_count"] == 0


def test_get_site_detail_refuses_a_foreign_site_id(tmp_path: Path) -> None:
    with _two_accounts(tmp_path) as settings:
        alice = _invoke(settings, "alice", "search_library", query="向量")
        site_id = alice["items"][0]["site_id"]
        stolen = _invoke(settings, "bob", "get_site_detail", site_id=site_id)

    assert "error" in stolen
    assert "name" not in stolen


def test_propose_site_produces_a_draft_without_writing(tmp_path: Path) -> None:
    with _two_accounts(tmp_path) as settings:
        before = _site_count(settings)
        result = _invoke(
            settings,
            "bob",
            "propose_site",
            url="https://example.com",
            name="Example",
            description="示例站点",
            category="工具",
            tags=["示例", " "],
        )
        after = _site_count(settings)

    assert result["status"] == "awaiting_confirmation"
    assert result["draft"]["url"] == "https://example.com"
    assert result["draft"]["tags"] == ["示例"]
    # The confirmation gate is enforced by the tool, not by the prompt.
    assert after == before


def test_propose_site_rejects_a_non_http_scheme(tmp_path: Path) -> None:
    with _two_accounts(tmp_path) as settings:
        result = _invoke(
            settings,
            "bob",
            "propose_site",
            url="javascript:alert(1)",
            name="坏链接",
        )

    assert result["status"] == "rejected"


def test_propose_site_flags_an_existing_duplicate(tmp_path: Path) -> None:
    with _two_accounts(tmp_path) as settings:
        result = _invoke(
            settings,
            "alice",
            "propose_site",
            url="https://qdrant.tech",
            name="Qdrant",
        )

    assert result["duplicate"] is not None
    assert result["duplicate"]["name"] == "Qdrant 向量数据库"


def test_web_search_tool_is_absent_without_a_search_provider(tmp_path: Path) -> None:
    with _two_accounts(tmp_path) as settings:

        async def scenario() -> list[str]:
            database = Database(settings.database_url)
            try:
                context = AgentToolContext(
                    database=database,
                    settings=settings,
                    user_id="account-alice",
                )
                return sorted(_tool_map(context))
            finally:
                await database.dispose()

        names = asyncio.run(scenario())

    assert "web_search" not in names
    assert names == [
        "get_site_detail",
        "list_categories",
        "list_spaces",
        "list_tags",
        "propose_site",
        "search_library",
    ]
