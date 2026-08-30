from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from webhub.agent import tools as tools_module
from webhub.agent.provider_binding import ProviderBinding
from webhub.agent.tools import (
    SOURCE_LIBRARY,
    SOURCE_WEB,
    AgentToolContext,
    build_tools,
    deterministic_collection_text,
)
from webhub.agent.web_search import WebSearchResult
from webhub.config import Settings
from webhub.db.database import Database
from webhub.db.migrations import upgrade_database
from webhub.db.models import Site, SpaceMember, User
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
                user_id = await session.scalar(select(User.id).where(User.username == username))
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


def _set_alice_favicon(settings: Settings, favicon_url: str) -> None:
    async def scenario() -> None:
        database = Database(settings.database_url)
        try:
            async with database.sessions() as session:
                user_id = await session.scalar(select(User.id).where(User.username == "alice"))
                assert user_id is not None
                site = await session.scalar(select(Site).where(Site.user_id == user_id))
                assert site is not None
                site.favicon_url = favicon_url
                await session.commit()
        finally:
            await database.dispose()

    asyncio.run(scenario())


def _seed_alice_sites(settings: Settings, count: int) -> None:
    async def scenario() -> None:
        database = Database(settings.database_url)
        try:
            async with database.sessions() as session:
                user_id = await session.scalar(select(User.id).where(User.username == "alice"))
                assert user_id is not None
                category_id = await session.scalar(
                    select(Site.category_id).where(Site.user_id == user_id).limit(1)
                )
                assert category_id is not None
                last_position = int(
                    await session.scalar(
                        select(func.max(Site.position)).where(
                            Site.user_id == user_id,
                            Site.category_id == category_id,
                        )
                    )
                    or 0
                )
                for index in range(count):
                    url = f"https://example.com/agent-site-{index:03d}"
                    name = f"AI 工具 {index:03d}"
                    session.add(
                        Site(
                            user_id=user_id,
                            category_id=category_id,
                            name=name,
                            normalized_name=name.casefold(),
                            original_url=url,
                            identity_url=url,
                            position=last_position + index + 1,
                        )
                    )
                await session.commit()
        finally:
            await database.dispose()

    asyncio.run(scenario())


def test_collection_intent_never_consumes_an_action_word_inside_the_url_path() -> None:
    assert deterministic_collection_text("https://example.com/收藏") is None
    assert (
        deterministic_collection_text("https://example.com/收藏 收藏")
        == "https://example.com/收藏"
    )
    assert (
        deterministic_collection_text(
            "/存入 https://example.com/收藏",
            slash_command_name="/存入",
            slash_command_argument="https://example.com/收藏",
        )
        == "https://example.com/收藏"
    )


def test_search_library_returns_only_the_calling_accounts_sites(tmp_path: Path) -> None:
    with _two_accounts(tmp_path) as settings:
        _set_alice_favicon(settings, "https://qdrant.tech/favicon.ico")
        alice = _invoke(settings, "alice", "search_library", query="向量")
        bob = _invoke(settings, "bob", "search_library", query="向量")

    assert alice["source"] == SOURCE_LIBRARY
    assert [item["name"] for item in alice["items"]] == ["Qdrant 向量数据库"]
    assert alice["items"][0]["favicon_url"] == "https://qdrant.tech/favicon.ico"
    assert alice["items"][0]["description"] == "开源向量检索引擎"
    assert alice["items"][0]["category"]
    assert "pinned" in alice["items"][0]
    # Bob shares the database but must never see Alice's row.
    assert bob["items"] == []
    assert bob["matched_count"] == 0
    assert bob["can_offer_online"] is True


def test_external_recommendations_require_the_current_scope_and_real_search_result(
    tmp_path: Path,
) -> None:
    async def scenario(settings: Settings) -> tuple[Any, Any, Any]:
        database = Database(settings.database_url)
        try:
            async with database.sessions() as session:
                alice_id = await session.scalar(
                    select(User.id).where(User.username == "alice")
                )
            assert alice_id is not None

            async def present(context: AgentToolContext) -> Any:
                return await _tool_map(context)["present_website_recommendations"].ainvoke(
                    {
                        "type": "tool_call",
                        "id": "present-external",
                        "name": "present_website_recommendations",
                        "args": {
                            "items": [
                                {
                                    "name": "外部文档",
                                    "url": "https://docs.example.com/guide",
                                    "description": "外部资料",
                                }
                            ]
                        },
                    }
                )

            collection = await present(
                AgentToolContext(database=database, settings=settings, user_id=alice_id)
            )
            online_without_search = await present(
                AgentToolContext(
                    database=database,
                    settings=settings,
                    user_id=alice_id,
                    search_scope="online",
                )
            )
            online_with_search = AgentToolContext(
                database=database,
                settings=settings,
                user_id=alice_id,
                search_scope="online",
            )
            online_with_search.register_web_search_results(["https://docs.example.com/guide"])
            trusted = await present(online_with_search)
            return collection, online_without_search, trusted
        finally:
            await database.dispose()

    with _two_accounts(tmp_path) as settings:
        collection, online_without_search, trusted = asyncio.run(scenario(settings))

    assert collection.artifact["code"] == "external_recommendation_rejected"
    assert collection.artifact["items"] == []
    assert online_without_search.artifact["code"] == "external_recommendation_rejected"
    assert online_without_search.artifact["items"] == []
    assert trusted.artifact["source"] == SOURCE_WEB
    assert trusted.artifact["items"][0]["url"] == "https://docs.example.com/guide"


def test_external_recommendations_use_exact_public_search_urls(tmp_path: Path) -> None:
    async def scenario(settings: Settings) -> tuple[Any, Any]:
        database = Database(settings.database_url)
        try:
            async with database.sessions() as session:
                alice_id = await session.scalar(
                    select(User.id).where(User.username == "alice")
                )
            assert alice_id is not None
            context = AgentToolContext(
                database=database,
                settings=settings,
                user_id=alice_id,
                search_scope="online",
            )
            context.register_web_search_results(
                ["https://docs.example.com/guide", "http://127.0.0.1/private"]
            )
            tool = _tool_map(context)["present_website_recommendations"]

            async def present(url: str, tool_call_id: str) -> Any:
                return await tool.ainvoke(
                    {
                        "type": "tool_call",
                        "id": tool_call_id,
                        "name": "present_website_recommendations",
                        "args": {
                            "items": [
                                {
                                    "name": "外部文档",
                                    "url": url,
                                    "description": "外部资料",
                                }
                            ]
                        },
                    }
                )

            alias = await present("http://www.docs.example.com/guide", "scheme-alias")
            private = await present("http://127.0.0.1/private", "private-target")
            return alias, private
        finally:
            await database.dispose()

    with _two_accounts(tmp_path) as settings:
        scheme_alias, private_target = asyncio.run(scenario(settings))

    assert scheme_alias.artifact["code"] == "external_recommendation_rejected"
    assert private_target.artifact["code"] == "external_recommendation_rejected"


def test_web_search_requires_a_successful_library_attempt(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    calls: list[str] = []

    async def fake_search_web(
        _binding: ProviderBinding,
        query: str,
        *,
        limit: int,
    ) -> list[WebSearchResult]:
        calls.append(f"{query}:{limit}")
        return [
            WebSearchResult(title="本机", url="http://127.0.0.1/private", snippet="拒绝"),
            WebSearchResult(
                title="敏感链接",
                url="https://example.com/private?token=secret",
                snippet="拒绝",
            ),
            WebSearchResult(
                title="文档",
                url="https://Docs.Example.com:443/guide#section",
                snippet="资料",
            ),
        ]

    monkeypatch.setattr(tools_module, "search_web", fake_search_web)

    async def scenario(
        settings: Settings,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
        database = Database(settings.database_url)
        try:
            async with database.sessions() as session:
                alice_id = await session.scalar(
                    select(User.id).where(User.username == "alice")
                )
            assert alice_id is not None
            context = AgentToolContext(
                database=database,
                settings=settings,
                user_id=alice_id,
                search_binding=ProviderBinding(
                    kind="search",
                    provider="tavily",
                    config_id="search-config",
                    display_name="Tavily",
                    base_url="https://api.tavily.com",
                    model_name=None,
                    timeout_seconds=10,
                    api_key="test-key",
                ),
                search_scope="online",
            )
            tools = _tool_map(context)
            rejected = await tools["web_search"].ainvoke({"query": "React", "limit": 3})
            await tools["search_library"].ainvoke({"query": "React", "limit": 3})
            accepted = await tools["web_search"].ainvoke({"query": "React", "limit": 3})
            await tools["search_library"].ainvoke({"query": "向量", "limit": 3})
            unrelated_rejected = await tools["web_search"].ainvoke(
                {"query": "另一个主题", "limit": 3}
            )
            await tools["search_library"].ainvoke(
                {"query": "开源向量检索引擎", "limit": 3}
            )
            private_rejected = await tools["web_search"].ainvoke(
                {"query": "开源向量检索引擎", "limit": 3}
            )
            return rejected, accepted, unrelated_rejected, private_rejected
        finally:
            await database.dispose()

    with _two_accounts(tmp_path) as settings:
        rejected, accepted, unrelated_rejected, private_rejected = asyncio.run(
            scenario(settings)
        )

    assert rejected["items"] == []
    assert "必须先检索网址库" in rejected["error"]
    assert accepted["items"] == [
        {"title": "文档", "url": "https://docs.example.com/guide", "snippet": "资料"}
    ]
    assert "同一关键词" in unrelated_rejected["error"]
    assert "私有字段" in private_rejected["error"]
    assert calls == ["React:3"]


def test_protected_library_fragments_require_explicit_user_text(tmp_path: Path) -> None:
    with _two_accounts(tmp_path) as settings:
        database = Database(settings.database_url)
        try:
            context = AgentToolContext(
                database=database,
                settings=settings,
                user_id="account-alice",
                user_message="公开主题",
            )
            context.register_protected_search_values(
                [
                    "https://x.example/private/supersecret?token=SUPERSECRET",
                    "http://corp.internal/wiki/acme",
                    "客户 Acme 的内部控制台",
                    "AI",
                ]
            )

            assert context.search_query_exposes_protected_value("SUPERSECRET") is True
            assert context.search_query_exposes_protected_value("corp internal wiki") is True
            assert context.search_query_exposes_protected_value("Acme 控制台") is True
            assert context.search_query_exposes_protected_value("OpenAI tools") is False

            explicit = AgentToolContext(
                database=database,
                settings=settings,
                user_id="account-alice",
                user_message="请搜索 Acme 控制台",
            )
            explicit.register_protected_search_values(["客户 Acme 的内部控制台"])
            assert explicit.search_query_exposes_protected_value("Acme 控制台") is False
        finally:
            asyncio.run(database.dispose())


def test_complete_library_result_set_stays_scoped_and_presents_as_artifact(
    tmp_path: Path,
) -> None:
    with _two_accounts(tmp_path) as settings:
        _seed_alice_sites(settings, 24)

        async def scenario() -> tuple[dict[str, Any], Any, Any]:
            database = Database(settings.database_url)
            try:
                async with database.sessions() as session:
                    alice_id = await session.scalar(
                        select(User.id).where(User.username == "alice")
                    )
                    bob_id = await session.scalar(select(User.id).where(User.username == "bob"))
                    category_id = await session.scalar(
                        select(Site.category_id).where(Site.user_id == alice_id).limit(1)
                    )
                assert alice_id is not None and bob_id is not None and category_id is not None

                alice_context = AgentToolContext(
                    database=database,
                    settings=settings,
                    user_id=alice_id,
                )
                alice_tools = _tool_map(alice_context)
                search_result = await alice_tools["search_library"].ainvoke(
                    {
                        "query": "",
                        "category_id": category_id,
                        "include_all": True,
                        "limit": 8,
                    }
                )
                result_set_id = search_result["result_set_id"]
                presentation = await alice_tools["present_website_recommendations"].ainvoke(
                    {
                        "type": "tool_call",
                        "id": "present-all",
                        "name": "present_website_recommendations",
                        "args": {"result_set_id": result_set_id},
                    }
                )

                bob_context = AgentToolContext(
                    database=database,
                    settings=settings,
                    user_id=bob_id,
                )
                rejected = await _tool_map(bob_context)[
                    "present_website_recommendations"
                ].ainvoke(
                    {
                        "type": "tool_call",
                        "id": "present-foreign",
                        "name": "present_website_recommendations",
                        "args": {"result_set_id": result_set_id},
                    }
                )
                return search_result, presentation, rejected
            finally:
                await database.dispose()

        search_result, presentation, rejected = asyncio.run(scenario())

    assert search_result["matched_count"] == 25
    assert len(search_result["items"]) == 8
    assert search_result["complete_result_set"] is True
    assert "items" not in json.loads(presentation.content)
    assert json.loads(presentation.content)["presented_count"] == 25
    assert presentation.artifact["complete"] is True
    assert presentation.artifact["matched_count"] == 25
    assert len(presentation.artifact["items"]) == 25
    assert all(
        set(item) == {"site_id", "name", "url", "favicon_url"}
        for item in presentation.artifact["items"]
    )
    assert rejected.artifact["code"] == "result_set_unavailable"
    assert "items" not in rejected.artifact


def test_default_library_search_freezes_every_match_for_pagination(tmp_path: Path) -> None:
    with _two_accounts(tmp_path) as settings:
        _seed_alice_sites(settings, 105)

        async def scenario() -> tuple[dict[str, Any], Any]:
            database = Database(settings.database_url)
            try:
                async with database.sessions() as session:
                    alice_id = await session.scalar(
                        select(User.id).where(User.username == "alice")
                    )
                assert alice_id is not None
                context = AgentToolContext(
                    database=database,
                    settings=settings,
                    user_id=alice_id,
                )
                tool_map = _tool_map(context)
                # Omitting include_all is the normal Agent path for an
                # unspecified "recommend some" request.
                search_result = await tool_map["search_library"].ainvoke(
                    {"query": "AI", "limit": 8}
                )
                presentation = await tool_map[
                    "present_website_recommendations"
                ].ainvoke(
                    {
                        "type": "tool_call",
                        "id": "present-default-all",
                        "name": "present_website_recommendations",
                        "args": {"result_set_id": search_result["result_set_id"]},
                    }
                )
                return search_result, presentation
            finally:
                await database.dispose()

        search_result, presentation = asyncio.run(scenario())

    assert search_result["matched_count"] == 105
    assert len(search_result["items"]) == 8
    assert search_result["complete_result_set"] is True
    assert presentation.artifact["matched_count"] == 105
    assert len(presentation.artifact["items"]) == 105
    assert presentation.artifact["items"][0]["name"] == "AI 工具 000"


def test_presentation_recovers_only_the_complete_latest_preview(tmp_path: Path) -> None:
    with _two_accounts(tmp_path) as settings:
        _seed_alice_sites(settings, 10)

        async def scenario() -> tuple[Any, Any]:
            database = Database(settings.database_url)
            try:
                async with database.sessions() as session:
                    alice_id = await session.scalar(
                        select(User.id).where(User.username == "alice")
                    )
                assert alice_id is not None
                context = AgentToolContext(database=database, settings=settings, user_id=alice_id)
                tool_map = _tool_map(context)
                search_result = await tool_map["search_library"].ainvoke(
                    {"query": "AI", "limit": 8}
                )
                recovered = await tool_map["present_website_recommendations"].ainvoke(
                    {
                        "type": "tool_call",
                        "id": "present-preview-only",
                        "name": "present_website_recommendations",
                        "args": {"items": list(reversed(search_result["items"]))},
                    }
                )
                selected = await tool_map["present_website_recommendations"].ainvoke(
                    {
                        "type": "tool_call",
                        "id": "present-explicit-four",
                        "name": "present_website_recommendations",
                        "args": {"items": search_result["items"][:4]},
                    }
                )
                return recovered, selected
            finally:
                await database.dispose()

        recovered, selected = asyncio.run(scenario())

    assert recovered.artifact["complete"] is True
    assert recovered.artifact["matched_count"] == 10
    assert selected.artifact["matched_count"] == 4
    assert len(selected.artifact["items"]) == 4


def test_explicit_finite_request_cannot_expand_when_model_omits_include_all(
    tmp_path: Path,
) -> None:
    with _two_accounts(tmp_path) as settings:
        _seed_alice_sites(settings, 10)

        async def scenario() -> tuple[dict[str, Any], Any, dict[str, Any], Any]:
            database = Database(settings.database_url)
            try:
                async with database.sessions() as session:
                    alice_id = await session.scalar(
                        select(User.id).where(User.username == "alice")
                    )
                assert alice_id is not None

                implicit_context = AgentToolContext(
                    database=database,
                    settings=settings,
                    user_id=alice_id,
                    user_message="精选 4 个 AI 网站",
                )
                implicit_tools = _tool_map(implicit_context)
                implicit_search = await implicit_tools["search_library"].ainvoke(
                    {"query": "AI", "limit": 4}
                )
                implicit_presentation = await implicit_tools[
                    "present_website_recommendations"
                ].ainvoke(
                    {
                        "type": "tool_call",
                        "id": "present-implicit-finite",
                        "name": "present_website_recommendations",
                        "args": {"result_set_id": implicit_search["result_set_id"]},
                    }
                )

                explicit_context = AgentToolContext(
                    database=database,
                    settings=settings,
                    user_id=alice_id,
                    user_message="推荐 AI 网站",
                )
                explicit_tools = _tool_map(explicit_context)
                explicit_search = await explicit_tools["search_library"].ainvoke(
                    {"query": "AI", "limit": 4, "include_all": False}
                )
                explicit_presentation = await explicit_tools[
                    "present_website_recommendations"
                ].ainvoke(
                    {
                        "type": "tool_call",
                        "id": "present-explicit-finite",
                        "name": "present_website_recommendations",
                        "args": {"items": explicit_search["items"]},
                    }
                )
                return (
                    implicit_search,
                    implicit_presentation,
                    explicit_search,
                    explicit_presentation,
                )
            finally:
                await database.dispose()

        implicit_search, implicit_presentation, explicit_search, explicit_presentation = (
            asyncio.run(scenario())
        )

    assert implicit_search["matched_count"] == 10
    assert implicit_search["selected_count"] == 4
    assert implicit_search["result_set_id"]
    assert implicit_presentation.artifact["matched_count"] == 4
    assert len(implicit_presentation.artifact["items"]) == 4
    assert explicit_search["matched_count"] == 10
    assert "result_set_id" not in explicit_search
    assert explicit_presentation.artifact["matched_count"] == 4


def test_explicit_fifteen_request_uses_result_set_beyond_preview_and_item_limits(
    tmp_path: Path,
) -> None:
    with _two_accounts(tmp_path) as settings:
        _seed_alice_sites(settings, 20)

        async def scenario() -> tuple[dict[str, Any], Any]:
            database = Database(settings.database_url)
            try:
                async with database.sessions() as session:
                    alice_id = await session.scalar(
                        select(User.id).where(User.username == "alice")
                    )
                assert alice_id is not None
                context = AgentToolContext(
                    database=database,
                    settings=settings,
                    user_id=alice_id,
                    user_message="精选 15 个 AI 网站",
                )
                tool_map = _tool_map(context)
                search_result = await tool_map["search_library"].ainvoke(
                    {"query": "AI", "limit": 4, "include_all": False}
                )
                presentation = await tool_map[
                    "present_website_recommendations"
                ].ainvoke(
                    {
                        "type": "tool_call",
                        "id": "present-explicit-fifteen",
                        "name": "present_website_recommendations",
                        "args": {"result_set_id": search_result["result_set_id"]},
                    }
                )
                return search_result, presentation
            finally:
                await database.dispose()

        search_result, presentation = asyncio.run(scenario())

    assert search_result["matched_count"] == 20
    assert search_result["selected_count"] == 15
    assert len(search_result["items"]) == 4
    assert search_result["complete_result_set"] is True
    assert search_result["result_set_id"]
    assert presentation.artifact["complete"] is True
    assert presentation.artifact["matched_count"] == 15
    assert len(presentation.artifact["items"]) == 15


def test_finite_recommendation_intent_requires_an_actual_count_request() -> None:
    for message in (
        "精选 4 个 AI 网站",
        "精选四个 AI 网站",
        "只要 3 条结果",
        "推荐五家设计站点",
        "前4",
        "前 4 个网站",
        "Top 4 AI websites",
        "Recommend 15 AI websites",
        "不但要推荐 3 个网站，还要给出理由",
        "不仅推荐三个站点，还要按相关度排序",
    ):
        assert tools_module._user_requests_finite_recommendations(message), message

    for message in (
        "推荐 GPT-4 网站",
        "推荐 React 19 文档",
        "推荐 4K 视频网站",
        "推荐 2026 年开发工具",
        "找 Python 3 文档",
        "Recommend GPT-4 websites",
        "Find React 19 websites",
        "Find Python 3 websites",
        "不要只给 3 个，全部列出",
        "推荐时不要只给 3 个，全部列出",
        "请推荐不要只给3个网站，全部列出",
        "Do not recommend 3 websites, list all",
        "推荐 3 个 AI 网站，再推荐 5 个设计网站",
    ):
        assert not tools_module._user_requests_finite_recommendations(message), message


def test_oversized_complete_recommendations_return_a_bounded_error_artifact(
    tmp_path: Path,
) -> None:
    with _two_accounts(tmp_path) as settings:
        async def scenario() -> Any:
            database = Database(settings.database_url)
            try:
                context = AgentToolContext(
                    database=database,
                    settings=settings,
                    user_id="account-alice",
                )
                items = [
                    {
                        "site_id": f"site-{index:04d}",
                        "name": f"容量验证网站 {index:04d}",
                        "url": f"https://example-{index:04d}.com/path/to/resource",
                        "favicon_url": f"https://example-{index:04d}.com/favicon.ico",
                    }
                    for index in range(3_000)
                ]
                result_set_id = context.register_library_result_set(items, items[:8])
                return await _tool_map(context)["present_website_recommendations"].ainvoke(
                    {
                        "type": "tool_call",
                        "id": "present-oversized",
                        "name": "present_website_recommendations",
                        "args": {"result_set_id": result_set_id},
                    }
                )
            finally:
                await database.dispose()

        presentation = asyncio.run(scenario())

    assert presentation.artifact["code"] == "result_set_too_large"
    assert presentation.artifact["matched_count"] == 3_000
    assert "items" not in presentation.artifact
    assert json.loads(presentation.content)["matched_count"] == 3_000


def test_complete_recommendation_sort_prefers_an_exact_normalized_name() -> None:
    items = [
        {"site_id": "a", "name": "A Docs helper", "url": "https://a.example"},
        {"site_id": "b", "name": "  Ｄｏｃｓ  ", "url": "https://b.example"},
    ]

    ranked = sorted(items, key=lambda item: tools_module._recommendation_sort_key(item, "Docs"))

    assert ranked[0]["site_id"] == "b"


def test_present_recommendations_keeps_explicit_items_and_hides_artifact_from_model(
    tmp_path: Path,
) -> None:
    with _two_accounts(tmp_path) as settings:

        async def scenario() -> tuple[Any, Any]:
            database = Database(settings.database_url)
            try:
                async with database.sessions() as session:
                    alice_id = await session.scalar(
                        select(User.id).where(User.username == "alice")
                    )
                assert alice_id is not None
                context = AgentToolContext(database=database, settings=settings, user_id=alice_id)
                tool = _tool_map(context)["present_website_recommendations"]
                presentation = await tool.ainvoke(
                    {
                        "type": "tool_call",
                        "id": "present-explicit",
                        "name": "present_website_recommendations",
                        "args": {
                            "items": [
                                {
                                    "name": "Qdrant",
                                    "url": "https://qdrant.tech",
                                    "description": "向量数据库",
                                }
                            ]
                        },
                    }
                )
                invalid = await tool.ainvoke(
                    {
                        "type": "tool_call",
                        "id": "present-too-many",
                        "name": "present_website_recommendations",
                        "args": {
                            "items": [
                                {
                                    "name": f"站点 {index}",
                                    "url": f"https://private-{index}.example",
                                }
                                for index in range(13)
                            ]
                        },
                    }
                )
                return presentation, invalid
            finally:
                await database.dispose()

        presentation, invalid = asyncio.run(scenario())

    assert "items" not in json.loads(presentation.content)
    assert presentation.artifact["matched_count"] == 1
    assert presentation.artifact["items"][0]["site_id"]
    assert presentation.artifact["source"] == SOURCE_LIBRARY
    assert invalid.status == "error"
    assert json.loads(invalid.content) == {
        "code": "invalid_tool_arguments",
        "error": "工具参数不符合要求，请修正后重试。",
    }
    assert "private-0.example" not in invalid.content


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
        "get_bookmark_import_preview",
        "get_site_detail",
        "list_bookmark_imports",
        "list_categories",
        "list_spaces",
        "list_tags",
        "present_website_recommendations",
        "propose_bookmark_import",
        "propose_reclassify",
        "propose_site",
        "propose_site_update",
        "propose_sites",
        "propose_space_batch",
        "propose_space_membership",
        "search_library",
    ]


@contextmanager
def _account_with_space(tmp_path: Path) -> Iterator[Settings]:
    """Alice owns one site plus a "设计" Space; Bob owns nothing."""

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
        site = client.post(
            "/api/library/sites",
            json={"name": "Figma", "url": "https://figma.com", "description": "界面设计工具"},
            headers=ORIGIN,
        )
        assert site.status_code == 201, site.text
        space = client.post("/api/spaces", json={"name": "设计"}, headers=ORIGIN)
        assert space.status_code == 201, space.text
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


def _site_rows(settings: Settings) -> list[tuple[Any, ...]]:
    """Every field a write tool could touch, for before/after comparison."""

    async def scenario() -> list[tuple[Any, ...]]:
        database = Database(settings.database_url)
        try:
            async with database.sessions() as session:
                rows = await session.execute(
                    select(
                        Site.id,
                        Site.name,
                        Site.description,
                        Site.category_id,
                        Site.pinned,
                        Site.version,
                    ).order_by(Site.id)
                )
                return [tuple(row) for row in rows.all()]
        finally:
            await database.dispose()

    return asyncio.run(scenario())


def _member_count(settings: Settings) -> int:
    async def scenario() -> int:
        database = Database(settings.database_url)
        try:
            async with database.sessions() as session:
                return int(await session.scalar(select(func.count(SpaceMember.site_id))) or 0)
        finally:
            await database.dispose()

    return asyncio.run(scenario())


def _alice_site_id(settings: Settings) -> str:
    found = _invoke(settings, "alice", "search_library", query="Figma")
    return str(found["items"][0]["site_id"])


def test_propose_site_update_diffs_without_writing(tmp_path: Path) -> None:
    """The queue headline sentence: put Figma in the 设计 category and pin it."""

    with _account_with_space(tmp_path) as settings:
        site_id = _alice_site_id(settings)
        before = _site_rows(settings)
        result = _invoke(
            settings,
            "alice",
            "propose_site_update",
            site_id=site_id,
            category="设计",
            pinned=True,
        )
        after = _site_rows(settings)

    assert result["status"] == "awaiting_confirmation"
    draft = result["draft"]
    assert draft["kind"] == "site_update"
    assert draft["site_id"] == site_id
    # Only what actually differs is proposed; untouched fields stay out.
    assert draft["changes"] == {"category": "设计", "pinned": True}
    assert draft["before"]["pinned"] is False
    assert draft["after"]["category"] == "设计"
    assert draft["after"]["pinned"] is True
    assert draft["after"]["name"] == "Figma"
    # The optimistic-lock token travels with the draft.
    assert draft["expected_version"] >= 1
    # Nothing was written: every field of every row is identical.
    assert after == before


def test_propose_site_update_omitted_fields_are_left_alone(tmp_path: Path) -> None:
    with _account_with_space(tmp_path) as settings:
        site_id = _alice_site_id(settings)
        renamed = _invoke(
            settings,
            "alice",
            "propose_site_update",
            site_id=site_id,
            name="Figma 设计",
        )
        cleared = _invoke(
            settings,
            "alice",
            "propose_site_update",
            site_id=site_id,
            description="",
        )

    # A rename must not drag the description along with it.
    assert renamed["draft"]["changes"] == {"name": "Figma 设计"}
    assert renamed["draft"]["after"]["description"] == "界面设计工具"
    # An explicit empty string is a clear, and stays distinguishable from omission.
    assert cleared["draft"]["changes"] == {"description": ""}


def test_propose_site_update_reports_a_noop_instead_of_a_pointless_draft(tmp_path: Path) -> None:
    with _account_with_space(tmp_path) as settings:
        site_id = _alice_site_id(settings)
        result = _invoke(
            settings,
            "alice",
            "propose_site_update",
            site_id=site_id,
            name="Figma",
            pinned=False,
        )

    assert result["status"] == "noop"
    assert "draft" not in result


def test_propose_site_update_refuses_a_foreign_site_id(tmp_path: Path) -> None:
    with _account_with_space(tmp_path) as settings:
        site_id = _alice_site_id(settings)
        before = _site_rows(settings)
        stolen = _invoke(
            settings,
            "bob",
            "propose_site_update",
            site_id=site_id,
            name="被别人改掉",
            pinned=True,
        )
        after = _site_rows(settings)

    assert stolen["status"] == "rejected"
    assert "draft" not in stolen
    assert after == before


def test_propose_site_update_rejects_blank_required_fields(tmp_path: Path) -> None:
    with _account_with_space(tmp_path) as settings:
        site_id = _alice_site_id(settings)
        blank_name = _invoke(settings, "alice", "propose_site_update", site_id=site_id, name="   ")
        blank_category = _invoke(
            settings, "alice", "propose_site_update", site_id=site_id, category=" "
        )

    assert blank_name["status"] == "rejected"
    assert blank_category["status"] == "rejected"


def test_propose_site_update_normalizes_and_dedupes_tags(tmp_path: Path) -> None:
    with _account_with_space(tmp_path) as settings:
        site_id = _alice_site_id(settings)
        result = _invoke(
            settings,
            "alice",
            "propose_site_update",
            site_id=site_id,
            tags=["设计", "  设计  ", "UI", "ui", " ", "原型"],
        )

    assert result["draft"]["changes"]["tags"] == ["设计", "UI", "原型"]


def test_propose_space_batch_resolves_a_space_by_name_without_writing(
    tmp_path: Path,
) -> None:
    with _account_with_space(tmp_path) as settings:
        site_id = _alice_site_id(settings)
        before = _member_count(settings)
        result = _invoke(
            settings,
            "alice",
            "propose_space_batch",
            site_ids=[site_id],
            space_name="设计",
        )
        after = _member_count(settings)

    assert result["status"] == "awaiting_confirmation"
    draft = result["draft"]
    assert draft["kind"] == "space_batch"
    assert draft["target"]["mode"] == "existing"
    assert draft["target"]["space_name"] == "设计"
    assert draft["target"]["expected_version"] >= 1
    assert draft["sites"] == [
        {"site_id": site_id, "name": "Figma", "url": "https://figma.com"}
    ]
    assert draft["already_member_count"] == 0
    assert after == before == 0


def test_propose_space_membership_will_not_invent_a_space(tmp_path: Path) -> None:
    with _account_with_space(tmp_path) as settings:
        site_id = _alice_site_id(settings)
        result = _invoke(
            settings,
            "alice",
            "propose_space_membership",
            site_id=site_id,
            space="不存在的空间",
            action="remove",
        )

    # Creating a Space is itself a write; the tool refuses and lists what exists.
    assert result["status"] == "rejected"
    assert result["available_spaces"] == ["设计"]


def test_propose_space_membership_reports_a_noop_when_already_correct(tmp_path: Path) -> None:
    with _account_with_space(tmp_path) as settings:
        site_id = _alice_site_id(settings)
        result = _invoke(
            settings,
            "alice",
            "propose_space_membership",
            site_id=site_id,
            space="设计",
            action="remove",
        )

    assert result["status"] == "noop"
    assert "draft" not in result


def test_propose_space_membership_refuses_across_accounts(tmp_path: Path) -> None:
    with _account_with_space(tmp_path) as settings:
        site_id = _alice_site_id(settings)
        # Bob can neither name Alice's site nor see her Space.
        stolen_site = _invoke(
            settings,
            "bob",
            "propose_space_membership",
            site_id=site_id,
            space="设计",
            action="remove",
        )

    assert stolen_site["status"] == "rejected"
    assert "draft" not in stolen_site


def test_a_stale_update_draft_conflicts_instead_of_overwriting(tmp_path: Path) -> None:
    """The draft carries the version it saw; a later edit must not be clobbered.

    This walks the whole loop the browser walks: propose (read-only, captures
    ``expected_version``), someone else edits the site, then the confirmation
    PATCH replays the captured version.  The point of the optimistic lock is
    that the second write loses rather than silently erasing the first.
    """

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
            json={"name": "Figma", "url": "https://figma.com"},
            headers=ORIGIN,
        )
        assert created.status_code == 201, created.text
        site_id = created.json()["id"]

        draft = _invoke(
            settings,
            "alice",
            "propose_site_update",
            site_id=site_id,
            name="Figma 设计",
        )["draft"]
        captured_version = draft["expected_version"]

        # Somebody else (another tab, the library page) edits first.
        meanwhile = client.patch(
            f"/api/library/sites/{site_id}",
            json={"expected_version": captured_version, "description": "别处先改的说明"},
            headers=ORIGIN,
        )
        assert meanwhile.status_code == 200, meanwhile.text

        # Confirming the now-stale draft must fail rather than overwrite.
        confirmed = client.patch(
            f"/api/library/sites/{site_id}",
            json={"expected_version": captured_version, "name": draft["changes"]["name"]},
            headers=ORIGIN,
        )
        assert confirmed.status_code == 409
        assert confirmed.json()["detail"]["code"] == "version_conflict"

        current = client.get(f"/api/library/sites/{site_id}").json()
        assert current["name"] == "Figma"
        assert current["description"] == "别处先改的说明"


def test_propose_sites_extracts_every_url_without_the_model_looping(tmp_path: Path) -> None:
    """The queue's acceptance case, at the tool boundary."""

    with _account_with_space(tmp_path) as settings:
        before = _site_rows(settings)
        result = _invoke(
            settings,
            "alice",
            "propose_sites",
            text=(
                "帮我把这些都存了：https://a.example.com/1 https://b.example.com/2\n"
                "还有 https://figma.com （这个已经有了）\n"
                "以及一个坏的 ftp://nope.example.com/x\n"
                "重复一次 https://a.example.com/1"
            ),
        )
        after = _site_rows(settings)

    assert result["status"] == "awaiting_confirmation"
    draft = result["draft"]
    assert draft["kind"] == "site_batch"
    # Three distinct http(s) URLs survive extraction; the ftp one never counts
    # as a URL at all, and the repeat is collapsed textually.
    assert draft["total"] == 3
    assert draft["ready"] == 2
    assert draft["duplicate"] == 1
    assert set(draft["urls"]) == {"https://a.example.com/1", "https://b.example.com/2"}
    # Nothing was written: a draft is a draft.
    assert after == before


def test_propose_sites_reports_noop_when_everything_is_already_saved(
    tmp_path: Path,
) -> None:
    with _account_with_space(tmp_path) as settings:
        result = _invoke(
            settings,
            "alice",
            "propose_sites",
            text="再存一次 https://figma.com",
        )
    assert result["status"] == "noop"
    assert "draft" not in result


def test_propose_sites_rejects_text_without_any_url(tmp_path: Path) -> None:
    with _account_with_space(tmp_path) as settings:
        result = _invoke(settings, "alice", "propose_sites", text="帮我存一下那个网站 example.com")
    assert result["status"] == "rejected"
    assert "draft" not in result


def test_propose_sites_is_account_scoped(tmp_path: Path) -> None:
    """Bob must not learn that Alice already has figma."""

    with _account_with_space(tmp_path) as settings:
        alice = _invoke(settings, "alice", "propose_sites", text="https://figma.com")
        bob = _invoke(settings, "bob", "propose_sites", text="https://figma.com")

    assert alice["status"] == "noop"
    assert bob["status"] == "awaiting_confirmation"
    assert bob["draft"]["ready"] == 1
