from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import unquote

from sqlalchemy import select

from webhub.chat import service as chat_service
from webhub.db.database import Database
from webhub.db.models import User
from webhub.portability.validation import validate_backup

from .conftest import Environment
from .test_import import apply, preview


def _seed(env: Environment) -> list[str]:
    category = env.post("/api/library/categories", {"name": "阅读资料", "icon": "BookOpen"})
    tag = env.post("/api/library/tags", {"name": "阅读"})
    sites = [
        env.post(
            "/api/library/sites",
            {
                "name": f"资料 {index}",
                "url": f"https://example.org/articles/{index}",
                "category_id": category["id"],
                "tag_ids": [tag["id"]],
                "description": f"资料说明 {index}",
            },
        )
        for index in range(2)
    ]
    space = env.post("/api/spaces", {"name": "阅读顺序"})
    for site in reversed(sites):
        added = env.post(
            f"/api/spaces/{space['id']}/members",
            {"site_id": site["id"], "expected_version": space["version"]},
        )
        space = added["space"]
    feed = env.post("/api/feeds", {"url": "https://example.org/rss"})
    env.execute("UPDATE feeds SET enabled=0,title='阅读周刊',kind='podcast' WHERE id=?", feed["id"])
    env.execute(
        "UPDATE sites SET meta_json=?,open_count=7,seen_at='2026-10-01',"
        "starred_at='2026-10-02',resurface_muted=1",
        json.dumps({"github": {"etag": "private-cache-value", "stars": 13}}),
    )
    return [site["id"] for site in sites]


def test_export_whitelist_long_conversation_and_account_isolation(env: Environment) -> None:
    _seed(env)

    async def conversation() -> None:
        database = Database(env.database_url)
        try:
            async with database.sessions() as session:
                user_id = await session.scalar(select(User.id).where(User.username == "alpha"))
                row = await chat_service.create_conversation(session, user_id, title="公开对话")
                for role, content in (
                    ("user", "x" * 150_000),
                    ("assistant", "可见答复"),
                    ("tool", "private-tool-output"),
                    ("system", "private-prompt"),
                ):
                    await chat_service.append_message(
                        session,
                        user_id,
                        row.id,
                        role=role,
                        content=content,
                        parts=[{"reasoning": "private-reasoning"}],
                        metadata={"private_execution": "private-raw-output"},
                    )
        finally:
            await database.dispose()

    asyncio.run(conversation())
    response = env.client.get("/api/portability/export")
    assert response.status_code == 200
    assert 'attachment; filename="webhub-backup-alpha-' in response.headers["Content-Disposition"]
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert set(payload) == {
        "format",
        "version",
        "exported_at",
        "account",
        "categories",
        "tags",
        "sites",
        "spaces",
        "feeds",
        "conversations",
    }
    assert set(payload["sites"][0]) == {
        "key",
        "url",
        "name",
        "summary",
        "description",
        "kind",
        "kind_source",
        "category_key",
        "tag_keys",
        "pinned",
        "favicon_url",
        "preview_url",
        "meta",
        "created_at",
        "open_count",
        "last_opened_at",
        "seen_at",
        "starred_at",
        "resurface_muted",
    }
    for excluded in (
        "secret",
        "token",
        "session",
        "audit",
        "payload_path",
        "bookmark-imports",
        "private-cache-value",
        "private-reasoning",
        "private-raw-output",
        "private-tool-output",
        "private-prompt",
        "provider_configs",
        "parts_json",
    ):
        assert excluded not in response.text
    assert [message["role"] for message in payload["conversations"][0]["messages"]] == [
        "user",
        "assistant",
    ]
    assert len(payload["conversations"][0]["messages"][0]["text"]) == 150_000
    validate_backup(response.content)
    assert preview(env, payload)["counts"]["conversations"]["skipped"] == 1
    env.register("beta")
    other = env.client.get("/api/portability/export").json()
    for field in ("sites", "tags", "spaces", "feeds", "conversations"):
        assert other[field] == []


def test_export_import_roundtrip_preserves_relationships_and_order(env: Environment) -> None:
    _seed(env)
    source = env.client.get("/api/portability/export").json()
    env.register("beta")
    result = apply(env, preview(env, source))
    assert result["created"]["sites"] == 2
    target = env.client.get("/api/portability/export").json()

    def equivalent(backup: dict[str, Any]) -> dict[str, Any]:
        categories = {row["key"]: row["name"] for row in backup["categories"]}
        tags = {row["key"]: row["name"] for row in backup["tags"]}
        sites = {row["key"]: row["url"] for row in backup["sites"]}
        return {
            "categories": sorted((row["name"], row["icon"]) for row in backup["categories"]),
            "tags": sorted(tags.values()),
            "sites": sorted(
                (
                    row["url"],
                    row["name"],
                    row["description"],
                    row["kind"],
                    row["kind_source"],
                    categories[row["category_key"]],
                    sorted(tags[key] for key in row["tag_keys"]),
                    row["open_count"],
                    row["meta"],
                    row["seen_at"],
                    row["starred_at"],
                    row["resurface_muted"],
                )
                for row in backup["sites"]
            ),
            "spaces": [
                (row["name"], [sites[key] for key in row["site_keys"]]) for row in backup["spaces"]
            ],
            "feeds": backup["feeds"],
        }

    assert equivalent(target) == equivalent(source)
    assert {row["key"] for row in target["sites"]}.isdisjoint(
        {row["key"] for row in source["sites"]}
    )


def test_unicode_username_has_safe_download_filename(env: Environment) -> None:
    env.register("中文账号")
    response = env.client.get("/api/portability/export")
    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert disposition.isascii()
    assert "webhub-backup-中文账号-" in unquote(disposition)


def test_export_holds_sqlite_snapshot_across_stream_sections(env: Environment) -> None:
    from webhub.library import service as library_service
    from webhub.library.schemas import SiteCreateRequest
    from webhub.portability.export import export_backup

    async def scenario() -> None:
        database = Database(env.database_url)
        categories_read, writer_committed = asyncio.Event(), asyncio.Event()
        try:

            async def write() -> None:
                await categories_read.wait()
                async with database.sessions() as session:
                    user_id = await session.scalar(select(User.id).where(User.username == "alpha"))
                    category = await library_service.create_category(session, user_id, "并发新分类")
                    await library_service.create_site(
                        session,
                        user_id,
                        SiteCreateRequest(
                            name="并发收藏",
                            url="https://concurrent.example/",
                            category_id=category.id,
                        ),
                    )
                writer_committed.set()

            async def read() -> bytes:
                async with database.sessions() as session:
                    user = await session.scalar(select(User).where(User.username == "alpha"))
                    chunks = []
                    async for chunk in export_backup(session, user):
                        if chunk == b', "sites": [':
                            categories_read.set()
                            await asyncio.wait_for(writer_committed.wait(), 5)
                        chunks.append(chunk)
                    assert not session.in_transaction()
                    return b"".join(chunks)

            payload, _ = await asyncio.wait_for(asyncio.gather(read(), write()), 10)
            backup = validate_backup(payload)
            assert backup.sites == []
            assert all(category.name != "并发新分类" for category in backup.categories)
            async with database.sessions() as session:
                user = await session.scalar(select(User).where(User.username == "alpha"))
                stream = export_backup(session, user)
                await anext(stream)
                assert session.in_transaction()
                await stream.aclose()
                assert not session.in_transaction()
        finally:
            await database.dispose()

    asyncio.run(scenario())
    latest = env.client.get("/api/portability/export").json()
    assert [site["name"] for site in latest["sites"]] == ["并发收藏"]
