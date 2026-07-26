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
        "get_bookmark_import_preview",
        "get_site_detail",
        "list_bookmark_imports",
        "list_categories",
        "list_spaces",
        "list_tags",
        "propose_bookmark_import",
        "propose_site",
        "propose_site_update",
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
        blank_name = _invoke(
            settings, "alice", "propose_site_update", site_id=site_id, name="   "
        )
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


def test_propose_space_membership_resolves_a_space_by_name_without_writing(
    tmp_path: Path,
) -> None:
    with _account_with_space(tmp_path) as settings:
        site_id = _alice_site_id(settings)
        before = _member_count(settings)
        result = _invoke(
            settings,
            "alice",
            "propose_space_membership",
            site_id=site_id,
            space="设计",
            action="add",
        )
        after = _member_count(settings)

    assert result["status"] == "awaiting_confirmation"
    draft = result["draft"]
    assert draft["kind"] == "space_membership"
    assert draft["action"] == "add"
    assert draft["space_name"] == "设计"
    assert draft["site_name"] == "Figma"
    assert draft["expected_version"] >= 1
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
            action="add",
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
            action="add",
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
