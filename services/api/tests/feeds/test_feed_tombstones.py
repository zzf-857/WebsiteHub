"""Q51 清理与裁剪保留 GUID，所有用户读路径隐藏墓碑。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from webhub.feeds import service

from .test_feeds import ORIGIN, Environment, _add, _items, _ok, _register, _use, rss
from .test_feeds import environment as environment


def _cleanup(env: Environment, *, now: datetime) -> int:
    async def run() -> int:
        database = env.database()
        try:
            return await service.cleanup_items(database, now=now)
        finally:
            await database.dispose()

    return asyncio.run(run())


def test_undated_item_does_not_resurrect_after_cleanup(environment: Environment) -> None:
    env = environment
    _use(env.client, _register(env.client, "undated-reader"))
    url = "https://undated.example/rss"
    document = (
        '<rss version="2.0"><channel><title>Undated</title><item><guid>undated</guid>'
        "<title>私有标题</title><description>私有摘要</description><author>作者</author>"
        "<link>https://undated.example/1</link>"
        '<enclosure url="https://undated.example/audio.mp3" type="audio/mpeg"/>'
        "</item></channel></rss>"
    ).encode()
    env.fetcher.queue(url, _ok(document))
    feed = _add(env, url)
    item = _items(env)[0]
    env.execute(
        "UPDATE feed_items SET image_url = ?, duration_seconds = 123 WHERE id = ?",
        "https://undated.example/image.png",
        item["id"],
    )
    before = env.query("SELECT guid, feed_id, user_id, fetched_at, published_at FROM feed_items")
    assert _cleanup(env, now=datetime.now(UTC) + timedelta(days=31)) == 1
    env.fetcher.queue(url, _ok(document))
    response = env.client.post(f"/api/feeds/{feed['id']}/refresh", headers=ORIGIN)
    assert response.status_code == 200
    assert response.json()["item_count"] == 0
    assert _items(env) == []
    assert (
        env.query("SELECT guid, feed_id, user_id, fetched_at, published_at FROM feed_items")
        == before
    )
    assert env.query(
        "SELECT title, summary, author, url, audio_url, image_url, duration_seconds, "
        "purged_at IS NOT NULL FROM feed_items"
    ) == [("", "", None, None, None, None, None, 1)]
    assert (
        env.client.patch(
            f"/api/feeds/items/{item['id']}", json={"read": True}, headers=ORIGIN
        ).status_code
        == 404
    )
    assert env.client.post(f"/api/feeds/items/{item['id']}/save", headers=ORIGIN).status_code == 404


def test_trimmed_items_do_not_resurrect_and_saved_items_survive(
    environment: Environment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = environment
    _use(env.client, _register(env.client, "trim-reader"))
    url = "https://trim.example/rss"
    now = datetime.now(UTC)
    env.fetcher.queue(
        url,
        _ok(
            rss(
                [
                    {
                        "guid": "saved",
                        "title": "Saved",
                        "link": "https://trim.example/saved",
                        "date": now - timedelta(days=1),
                    },
                    {"guid": "old", "title": "Old", "date": now - timedelta(hours=12)},
                ]
            )
        ),
    )
    feed = _add(env, url)
    saved = next(item for item in _items(env) if item["title"] == "Saved")
    assert (
        env.client.post(f"/api/feeds/items/{saved['id']}/save", headers=ORIGIN).status_code == 200
    )
    site_before = env.query("SELECT id, version, updated_at FROM sites")
    monkeypatch.setattr(service, "MAX_ITEMS_PER_FEED", 1)
    document = rss(
        [
            {"guid": "old", "title": "Old", "date": now - timedelta(hours=12)},
            {"guid": "new", "title": "New", "date": now},
        ]
    )
    env.fetcher.queue(url, _ok(document), _ok(document))
    for _ in range(2):
        response = env.client.post(f"/api/feeds/{feed['id']}/refresh", headers=ORIGIN)
        assert response.status_code == 200
        assert response.json()["item_count"] == 2
    assert {item["title"] for item in _items(env)} == {"Saved", "New"}
    assert env.query("SELECT guid, purged_at IS NOT NULL FROM feed_items ORDER BY guid") == [
        ("new", 0),
        ("old", 1),
        ("saved", 0),
    ]
    assert _cleanup(env, now=now + timedelta(days=32)) == 1
    assert [item["title"] for item in _items(env)] == ["Saved"]
    assert env.query("SELECT id, version, updated_at FROM sites") == site_before


def test_tombstones_are_hard_deleted_only_after_one_year(environment: Environment) -> None:
    env = environment
    _use(env.client, _register(env.client, "year-reader"))
    url = "https://year.example/rss"
    env.fetcher.queue(
        url,
        _ok(
            rss(
                [
                    {"guid": guid, "title": guid}
                    for guid in (
                        "expired",
                        "boundary",
                        "recent",
                    )
                ]
            )
        ),
    )
    _add(env, url)
    now = datetime.now(UTC)
    for guid, age in (("expired", 366), ("boundary", 365), ("recent", 364)):
        env.execute(
            "UPDATE feed_items SET purged_at = ? WHERE guid = ?",
            (now - timedelta(days=age)).strftime("%Y-%m-%d %H:%M:%S.%f"),
            guid,
        )
    assert _cleanup(env, now=now) == 0
    assert env.query("SELECT guid FROM feed_items ORDER BY guid") == [("boundary",), ("recent",)]


def test_tombstones_are_absent_from_lists_picks_counts_and_mcp(environment: Environment) -> None:
    env = environment
    _use(env.client, _register(env.client, "digest-reader"))
    url = "https://digest.example/rss"
    env.fetcher.queue(
        url,
        _ok(
            rss(
                [
                    {"guid": "visible", "title": "Visible"},
                    {"guid": "purged", "title": "Purged"},
                ]
            )
        ),
    )
    feed = _add(env, url)
    env.execute(
        "UPDATE feed_items SET purged_at = ? WHERE guid = 'purged'",
        datetime.now(UTC).isoformat(" "),
    )
    assert [item["title"] for item in _items(env)] == ["Visible"]
    assert [
        item["title"]
        for item in env.client.get(f"/api/feeds/items?feed_id={feed['id']}").json()["items"]
    ] == ["Visible"]
    assert [
        pick["item"]["title"] for pick in env.client.get("/api/feeds/picks").json()["items"]
    ] == [
        "Visible",
    ]
    assert env.client.get("/api/feeds").json()["items"][0]["item_count"] == 1
    token = env.client.post(
        "/api/agent-tokens", json={"name": "Q51", "scopes": ["read"]}, headers=ORIGIN
    ).json()["token"]
    env.client.cookies.clear()
    response = env.client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "get_feed_digest", "arguments": {"limit": 5}},
        },
        headers={
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
        },
    )
    assert response.status_code == 200
    payload = json.loads(response.json()["result"]["content"][0]["text"])
    assert [item["title"] for item in payload["items"]] == ["Visible"]
