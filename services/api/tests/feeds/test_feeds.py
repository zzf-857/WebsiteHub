"""Q50 RSS / 播客订阅流：解析去重、条件请求、退避、30 天清理、精选排序、收进收藏库、MCP。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from webhub.config import Settings
from webhub.db.database import Database
from webhub.db.migrations import upgrade_database
from webhub.feeds import fetch as feed_fetch
from webhub.feeds import scheduler
from webhub.feeds import service as feed_service
from webhub.feeds.fetch import FeedFetchResult
from webhub.feeds.parser import (
    MAX_ITEMS_PER_FETCH,
    FeedParseError,
    parse_duration,
    parse_feed,
)
from webhub.ingestion.worker import AnalysisSchedule
from webhub.main import create_app
from webhub.providers.targets import ProviderTargetError

ORIGIN = {"Origin": "http://testserver"}
COOKIE_NAME = "webhub_session"
NOW = datetime.now(UTC)


def _rfc822(value: datetime) -> str:
    return value.strftime("%a, %d %b %Y %H:%M:%S GMT")


def rss(items: list[dict[str, Any]], *, title: str = "Example Blog") -> bytes:
    body = []
    for item in items:
        parts = [f"<title>{item['title']}</title>"]
        if "link" in item:
            parts.append(f"<link>{item['link']}</link>")
        if "guid" in item:
            parts.append(f"<guid>{item['guid']}</guid>")
        parts.append(f"<pubDate>{_rfc822(item.get('date', NOW))}</pubDate>")
        if "description" in item:
            parts.append(f"<description>{item['description']}</description>")
        if "audio" in item:
            parts.append(f'<enclosure url="{item["audio"]}" type="audio/mpeg" length="1"/>')
            parts.append(f"<itunes:duration>{item.get('duration', '45:00')}</itunes:duration>")
        body.append("<item>" + "".join(parts) + "</item>")
    return (
        '<?xml version="1.0"?><rss version="2.0" '
        'xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel>'
        f"<title>{title}</title><link>https://blog.example/</link>"
        + "".join(body)
        + "</channel></rss>"
    ).encode()


# ------------------------------------------------------------------ parser


def test_parser_strips_markup_rejects_unsafe_links_and_dedupes() -> None:
    parsed = parse_feed(
        rss(
            [
                {
                    "title": "Hello &lt;b&gt;world&lt;/b&gt;",
                    "link": "https://blog.example/a",
                    "guid": "a",
                    "description": (
                        "&lt;p&gt;Body &lt;script&gt;alert(1)&lt;/script&gt;text&lt;/p&gt;"
                    ),
                },
                {"title": "Dup", "link": "https://blog.example/a2", "guid": "a"},
                {"title": "Evil", "link": "javascript:alert(1)", "guid": "b"},
            ]
        )
    )
    assert parsed.title == "Example Blog"
    assert parsed.site_url == "https://blog.example/"
    assert parsed.kind == "rss"
    assert [item.guid for item in parsed.items] == ["a", "b"]
    first, evil = parsed.items
    assert first.title == "Hello world"
    assert first.summary == "Body text"
    assert first.published_at is not None and first.published_at.tzinfo is not None
    assert evil.url is None


def test_parser_reads_atom_and_podcasts_and_caps_items() -> None:
    atom = (
        b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>Atom</title>'
        b'<link href="https://atom.example/"/><entry><id>urn:1</id><title>E1</title>'
        b'<link href="https://atom.example/1"/><updated>2026-10-20T10:00:00Z</updated>'
        b"<summary>S</summary></entry></feed>"
    )
    parsed = parse_feed(atom)
    assert parsed.items[0].guid == "urn:1"
    assert parsed.items[0].url == "https://atom.example/1"

    podcast = parse_feed(
        rss(
            [
                {
                    "title": "Ep",
                    "guid": "e1",
                    "audio": "https://cdn.example/e1.mp3",
                    "duration": "1:02:03",
                }
            ]
        )
    )
    assert podcast.kind == "podcast"
    assert podcast.items[0].audio_url == "https://cdn.example/e1.mp3"
    assert podcast.items[0].duration_seconds == 3723

    many = parse_feed(rss([{"title": f"T{i}", "guid": f"g{i}"} for i in range(80)]))
    assert len(many.items) == MAX_ITEMS_PER_FETCH

    with pytest.raises(FeedParseError):
        parse_feed(b"<html><body>not a feed</body></html>")


def test_parse_duration_variants() -> None:
    assert parse_duration("3723") == 3723
    assert parse_duration("62:03") == 3723
    assert parse_duration("bogus") is None
    assert parse_duration("") is None


# ------------------------------------------------------------- fetch client


@dataclass
class FakeResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes = b""

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        for start in range(0, len(self.body), 65536):
            yield self.body[start : start + 65536]


def _install_stream(
    monkeypatch: pytest.MonkeyPatch, responses: dict[str, FakeResponse | Exception]
) -> list[tuple[str, dict[str, str]]]:
    calls: list[tuple[str, dict[str, str]]] = []

    @asynccontextmanager
    async def fake_stream(url: str, *, timeout_seconds: int, headers: dict[str, str]):
        calls.append((url, dict(headers)))
        response = responses[url]
        if isinstance(response, Exception):
            raise response
        yield url, response

    monkeypatch.setattr(feed_fetch, "pinned_public_stream", fake_stream)
    return calls


def test_fetch_sends_conditional_headers_and_handles_304(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_stream(monkeypatch, {"https://f.example/rss": FakeResponse(304, {})})
    result = asyncio.run(feed_fetch.fetch_feed("https://f.example/rss", '"v1"', "Tue, 20 Oct 2026"))
    assert result.status == "not_modified"
    assert calls[0][1]["if-none-match"] == '"v1"'
    assert calls[0][1]["if-modified-since"] == "Tue, 20 Oct 2026"


def test_fetch_follows_redirects_and_caps_body(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_stream(
        monkeypatch,
        {
            "https://f.example/old": FakeResponse(301, {"location": "/new"}),
            "https://f.example/new": FakeResponse(
                200, {"etag": '"v2"', "last-modified": "Wed, 21 Oct 2026"}, b"<rss/>"
            ),
        },
    )
    result = asyncio.run(feed_fetch.fetch_feed("https://f.example/old", None, None))
    assert result == FeedFetchResult(
        "ok", body=b"<rss/>", etag='"v2"', last_modified="Wed, 21 Oct 2026"
    )

    _install_stream(
        monkeypatch,
        {"https://f.example/big": FakeResponse(200, {}, b"x" * (feed_fetch.MAX_FEED_BYTES + 10))},
    )
    assert asyncio.run(feed_fetch.fetch_feed("https://f.example/big", None, None)).error == (
        "too_large"
    )

    _install_stream(
        monkeypatch,
        {"https://f.example/loop": FakeResponse(302, {"location": "javascript:x"})},
    )
    assert asyncio.run(feed_fetch.fetch_feed("https://f.example/loop", None, None)).error == (
        "unsafe_target"
    )


def test_fetch_reports_unsafe_targets_and_server_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_stream(
        monkeypatch,
        {
            "http://127.0.0.1/rss": ProviderTargetError("resource_private", "private"),
            "https://f.example/down": FakeResponse(503, {}),
            "https://f.example/gone": FakeResponse(404, {}),
            "https://f.example/boom": RuntimeError("socket"),
        },
    )
    run = lambda url: asyncio.run(feed_fetch.fetch_feed(url, None, None)).error  # noqa: E731
    assert run("http://127.0.0.1/rss") == "unsafe_target"
    assert run("https://f.example/down") == "http_unavailable"
    assert run("https://f.example/gone") == "http_error"
    assert run("https://f.example/boom") == "network_error"


def test_only_configured_origins_may_be_private(monkeypatch: pytest.MonkeyPatch) -> None:
    public = _install_stream(monkeypatch, {"http://rsshub:1200/x": FakeResponse(304, {})})
    private_calls: list[tuple[str, bool]] = []

    @asynccontextmanager
    async def fake_private(url: str, *, timeout_seconds: int, allow_private: bool, headers):
        private_calls.append((url, allow_private))
        yield url, FakeResponse(304, {})

    monkeypatch.setattr(feed_fetch, "_pinned_stream", fake_private)
    monkeypatch.delenv(feed_fetch.TRUSTED_ORIGINS_ENV, raising=False)
    asyncio.run(feed_fetch.fetch_feed("http://rsshub:1200/x", None, None))
    assert len(public) == 1 and private_calls == []

    monkeypatch.setenv(feed_fetch.TRUSTED_ORIGINS_ENV, "http://RSSHub:1200 , bogus")
    asyncio.run(feed_fetch.fetch_feed("http://rsshub:1200/x", None, None))
    assert private_calls == [("http://rsshub:1200/x", True)]
    # A different port on the same host is not trusted.
    public2 = _install_stream(monkeypatch, {"http://rsshub:1201/x": FakeResponse(304, {})})
    asyncio.run(feed_fetch.fetch_feed("http://rsshub:1201/x", None, None))
    assert len(public2) == 1 and len(private_calls) == 1


def test_backoff_doubles_and_caps() -> None:
    assert feed_service.backoff_delay(0) == timedelta(hours=1)
    assert feed_service.backoff_delay(1) == timedelta(hours=2)
    assert feed_service.backoff_delay(2) == timedelta(hours=4)
    assert feed_service.backoff_delay(9) == timedelta(hours=24)


# --------------------------------------------------------------- API level


@dataclass
class FakeFetcher:
    responses: dict[str, list[FeedFetchResult]] = field(default_factory=dict)
    calls: list[tuple[str, str | None, str | None]] = field(default_factory=list)

    def queue(self, url: str, *results: FeedFetchResult) -> None:
        self.responses.setdefault(url, []).extend(results)

    async def __call__(
        self, url: str, etag: str | None, last_modified: str | None
    ) -> FeedFetchResult:
        self.calls.append((url, etag, last_modified))
        queue = self.responses.get(url) or []
        return queue.pop(0) if queue else FeedFetchResult("error", error="network_error")


@dataclass
class Environment:
    client: TestClient
    database_path: Path
    database_url: str
    fetcher: FakeFetcher
    scheduled: list[tuple[str, tuple[str, ...]]]

    def database(self) -> Database:
        return Database(self.database_url)

    def query(self, sql: str, *params: object) -> list[tuple[Any, ...]]:
        with sqlite3.connect(self.database_path) as connection:
            return connection.execute(sql, params).fetchall()

    def execute(self, sql: str, *params: object) -> None:
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(sql, params)
            connection.commit()


@pytest.fixture
def environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Environment]:
    database_path = tmp_path / "main.sqlite3"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    settings = Settings(environment="test", database_url=database_url, data_directory=tmp_path)
    upgrade_database(settings.database_url)
    scheduled: list[tuple[str, tuple[str, ...]]] = []

    def fake_schedule(*_args: object, **kwargs: Any) -> AnalysisSchedule:
        scheduled.append((str(kwargs["user_id"]), tuple(kwargs["site_ids"])))
        return AnalysisSchedule(queued=1, already_queued=0, rejected=0)

    monkeypatch.setattr("webhub.ingestion.worker.schedule_analysis", fake_schedule)
    monkeypatch.setattr(
        "webhub.ingestion.worker.ensure_auto_backfill", lambda *_args, **_kwargs: False
    )
    fetcher = FakeFetcher()
    monkeypatch.setattr(feed_fetch, "fetch_feed", fetcher)
    with TestClient(create_app(settings=settings)) as client:
        yield Environment(client, database_path, database_url, fetcher, scheduled)


def _register(client: TestClient, username: str) -> str:
    response = client.post(
        "/api/auth/register",
        json={"username": username, "password": "a sufficiently secure password"},
        headers=ORIGIN,
    )
    assert response.status_code == 201, response.text
    cookie = response.cookies.get(COOKIE_NAME)
    assert cookie
    return cookie


def _use(client: TestClient, cookie: str) -> None:
    client.cookies.clear()
    client.cookies.set(COOKIE_NAME, cookie)


def _ok(document: bytes, etag: str | None = '"v1"') -> FeedFetchResult:
    return FeedFetchResult("ok", body=document, etag=etag, last_modified="Tue, 20 Oct 2026")


def _add(env: Environment, url: str) -> dict[str, Any]:
    response = env.client.post("/api/feeds", json={"url": url}, headers=ORIGIN)
    assert response.status_code == 201, response.text
    return response.json()


def _items(env: Environment) -> list[dict[str, Any]]:
    response = env.client.get("/api/feeds/items")
    assert response.status_code == 200, response.text
    return response.json()["items"]


def test_add_feed_fetches_dedupes_and_uses_conditional_requests(environment: Environment) -> None:
    env = environment
    _use(env.client, _register(env.client, "reader"))
    url = "https://blog.example/rss"
    first = rss(
        [
            {"title": "One", "link": "https://blog.example/1", "guid": "1"},
            {"title": "Two", "link": "https://blog.example/2", "guid": "2"},
        ]
    )
    env.fetcher.queue(url, _ok(first))
    feed = _add(env, url)
    assert feed["title"] == "Example Blog"
    assert feed["kind"] == "rss"
    assert feed["item_count"] == 2
    assert feed["last_error"] is None
    assert env.fetcher.calls[-1] == (url, None, None)

    # Same document again plus one new entry: no duplicates, ETag sent back.
    env.fetcher.queue(
        url,
        _ok(
            first.replace(
                b"</channel>", b"<item><title>Three</title><guid>3</guid></item></channel>"
            )
        ),
        FeedFetchResult("not_modified"),
    )
    refreshed = env.client.post(f"/api/feeds/{feed['id']}/refresh", headers=ORIGIN).json()
    assert refreshed["item_count"] == 3
    assert env.fetcher.calls[-1] == (url, '"v1"', "Tue, 20 Oct 2026")
    refreshed = env.client.post(f"/api/feeds/{feed['id']}/refresh", headers=ORIGIN).json()
    assert refreshed["item_count"] == 3
    assert refreshed["consecutive_failures"] == 0

    duplicate = env.client.post("/api/feeds", json={"url": url}, headers=ORIGIN)
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "duplicate_feed"


def test_failures_back_off_and_reenable_resets(environment: Environment) -> None:
    env = environment
    _use(env.client, _register(env.client, "backoff"))
    url = "https://down.example/rss"
    env.fetcher.queue(url, FeedFetchResult("error", error="http_unavailable"))
    feed = _add(env, url)
    assert feed["consecutive_failures"] == 1
    assert feed["last_error"] == "http_unavailable"
    fetched = datetime.fromisoformat(feed["last_fetched_at"])
    assert datetime.fromisoformat(feed["next_fetch_at"]) - fetched == timedelta(hours=2)

    env.fetcher.queue(url, _ok(b"<html>not a feed</html>"))
    feed = env.client.post(f"/api/feeds/{feed['id']}/refresh", headers=ORIGIN).json()
    assert feed["consecutive_failures"] == 2
    assert feed["last_error"] == "not_a_feed"
    fetched = datetime.fromisoformat(feed["last_fetched_at"])
    assert datetime.fromisoformat(feed["next_fetch_at"]) - fetched == timedelta(hours=4)

    # Not due yet: the poller leaves it alone.
    database = env.database()
    try:
        assert asyncio.run(feed_service.poll_due_feeds(database, fetcher=env.fetcher)) == 0
        disabled = env.client.patch(
            f"/api/feeds/{feed['id']}", json={"enabled": False}, headers=ORIGIN
        ).json()
        assert disabled["enabled"] is False
        later = datetime.now(UTC) + timedelta(days=2)
        assert asyncio.run(feed_service.due_feed_ids(database, now=later)) == []
        enabled = env.client.patch(
            f"/api/feeds/{feed['id']}", json={"enabled": True}, headers=ORIGIN
        ).json()
        assert enabled["consecutive_failures"] == 0
        env.fetcher.queue(url, _ok(rss([{"title": "Back", "guid": "b"}])))
        assert asyncio.run(feed_service.poll_due_feeds(database, fetcher=env.fetcher)) == 1
    finally:
        asyncio.run(database.dispose())
    assert env.client.get("/api/feeds").json()["items"][0]["item_count"] == 1


def test_cleanup_drops_old_items_but_keeps_saved_ones(environment: Environment) -> None:
    env = environment
    _use(env.client, _register(env.client, "cleaner"))
    url = "https://old.example/rss"
    env.fetcher.queue(
        url,
        _ok(
            rss(
                [
                    {"title": "Fresh", "link": "https://old.example/f", "guid": "f"},
                    {"title": "Old", "link": "https://old.example/o", "guid": "o"},
                    {"title": "Saved", "link": "https://old.example/s", "guid": "s"},
                    {
                        "title": "Ancient",
                        "link": "https://old.example/a",
                        "guid": "a",
                        "date": NOW - timedelta(days=60),
                    },
                ]
            )
        ),
    )
    _add(env, url)
    titles = {item["title"]: item for item in _items(env)}
    # Already past retention when fetched: never stored.
    assert set(titles) == {"Fresh", "Old", "Saved"}
    saved = env.client.post(f"/api/feeds/items/{titles['Saved']['id']}/save", headers=ORIGIN)
    assert saved.status_code == 200, saved.text
    stale = (datetime.now(UTC) - timedelta(days=31)).strftime("%Y-%m-%d %H:%M:%S.%f")
    env.execute(
        "UPDATE feed_items SET published_at = ?, fetched_at = ? WHERE title IN ('Old', 'Saved')",
        stale,
        stale,
    )
    database = env.database()
    try:
        asyncio.run(scheduler.tick(database))
        assert asyncio.run(feed_service.cleanup_items(database)) == 0
    finally:
        asyncio.run(database.dispose())
    assert {item["title"] for item in _items(env)} == {"Fresh", "Saved"}


def test_daily_picks_rank_by_library_tags_with_source_cap(environment: Environment) -> None:
    env = environment
    _use(env.client, _register(env.client, "picker"))
    tag = env.client.post("/api/library/tags", json={"name": "Rust"}, headers=ORIGIN).json()
    for index in range(3):
        response = env.client.post(
            "/api/library/sites",
            json={"name": f"R{index}", "url": f"https://r{index}.example/", "tag_ids": [tag["id"]]},
            headers=ORIGIN,
        )
        assert response.status_code == 201, response.text

    busy = "https://busy.example/rss"
    env.fetcher.queue(
        busy,
        _ok(
            rss(
                [
                    {"title": f"Rust tip {i}", "link": f"https://busy.example/{i}", "guid": f"b{i}"}
                    for i in range(6)
                ]
                + [{"title": "Trust me", "link": "https://busy.example/t", "guid": "t"}],
                title="Busy",
            )
        ),
    )
    _add(env, busy)
    quiet = "https://quiet.example/rss"
    env.fetcher.queue(
        quiet,
        _ok(
            rss(
                [
                    {
                        "title": "Weekly notes",
                        "link": "https://quiet.example/w",
                        "guid": "w",
                        "description": "a short rust section",
                        "date": NOW - timedelta(days=2),
                    },
                    {"title": "Gardening", "link": "https://quiet.example/g", "guid": "g"},
                ],
                title="Quiet",
            )
        ),
    )
    _add(env, quiet)

    picks = env.client.get("/api/feeds/picks").json()["items"]
    per_source: dict[str, int] = {}
    for pick in picks:
        per_source[pick["feed_title"]] = per_source.get(pick["feed_title"], 0) + 1
    assert per_source["Busy"] == 3
    titles = [pick["item"]["title"] for pick in picks]
    assert all(title.startswith("Rust tip") for title in titles[:3])
    assert "rust" in picks[0]["matched"]
    assert "「rust」" in picks[0]["reason"]
    # The summary match beats an unrelated fresh item from the same source.
    assert titles.index("Weekly notes") < titles.index("Gardening")
    # "Trust" must not count as "rust".
    assert "Trust me" not in titles[:3]

    dismissed = picks[0]["item"]["id"]
    response = env.client.patch(
        f"/api/feeds/items/{dismissed}", json={"dismissed": True}, headers=ORIGIN
    )
    assert response.json()["dismissed"] is True
    again = [pick["item"]["id"] for pick in env.client.get("/api/feeds/picks").json()["items"]]
    assert dismissed not in again
    assert (
        len(
            [
                1
                for pick in env.client.get("/api/feeds/picks").json()["items"]
                if pick["feed_title"] == "Busy"
            ]
        )
        == 3
    )


def test_score_prefers_title_matches_and_recent_items() -> None:
    keywords = [feed_service.Keyword("ai", 2.0, True), feed_service.Keyword("设计", 1.0, False)]
    fresh, matched = feed_service.score_item(
        title="New AI model", summary="", published=NOW, now=NOW, keywords=keywords
    )
    assert matched == ["ai"]
    stale, _ = feed_service.score_item(
        title="New AI model",
        summary="",
        published=NOW - timedelta(days=5),
        now=NOW,
        keywords=keywords,
    )
    summary_only, _ = feed_service.score_item(
        title="Other", summary="关于设计的思考", published=NOW, now=NOW, keywords=keywords
    )
    said, matched_said = feed_service.score_item(
        title="He said so", summary="", published=NOW, now=NOW, keywords=keywords
    )
    assert fresh > stale
    assert fresh > summary_only > said
    assert matched_said == []


def test_save_to_library_creates_links_and_is_idempotent(environment: Environment) -> None:
    env = environment
    _use(env.client, _register(env.client, "saver"))
    url = "https://pod.example/rss"
    env.fetcher.queue(
        url,
        _ok(
            rss(
                [
                    {
                        "title": "Episode 1",
                        "link": "https://pod.example/e1",
                        "guid": "e1",
                        "audio": "https://cdn.pod.example/e1.mp3",
                        "description": "About things",
                    },
                    {"title": "Known", "link": "https://known.example/", "guid": "k"},
                    {"title": "No link", "guid": "n"},
                ],
                title="Pod",
            )
        ),
    )
    feed = _add(env, url)
    assert feed["kind"] == "podcast"
    items = {item["title"]: item for item in _items(env)}
    assert items["Episode 1"]["duration_seconds"] == 45 * 60
    assert items["Episode 1"]["audio_url"] == "https://cdn.pod.example/e1.mp3"

    saved = env.client.post(f"/api/feeds/items/{items['Episode 1']['id']}/save", headers=ORIGIN)
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["created"] is True
    assert body["item"]["saved_site_id"] == body["site_id"]
    assert body["item"]["read"] is True
    site = env.client.get(f"/api/library/sites/{body['site_id']}").json()
    assert site["name"] == "Episode 1"
    assert site["original_url"] == "https://pod.example/e1"
    assert env.scheduled[-1][1] == (body["site_id"],)

    again = env.client.post(f"/api/feeds/items/{items['Episode 1']['id']}/save", headers=ORIGIN)
    assert again.json() == {**body, "created": False}

    existing = env.client.post(
        "/api/library/sites",
        json={"name": "Known", "url": "https://known.example/"},
        headers=ORIGIN,
    ).json()
    linked = env.client.post(f"/api/feeds/items/{items['Known']['id']}/save", headers=ORIGIN)
    assert linked.json()["site_id"] == existing["id"]
    assert linked.json()["created"] is False

    no_link = env.client.post(f"/api/feeds/items/{items['No link']['id']}/save", headers=ORIGIN)
    assert no_link.status_code == 422
    assert no_link.json()["detail"]["code"] == "item_without_url"

    # Deleting the resource unlinks the item instead of deleting it.
    response = env.client.delete(
        f"/api/library/sites/{body['site_id']}?expected_version={site['version']}",
        headers=ORIGIN,
    )
    assert response.status_code == 200, response.text
    assert env.query(
        "SELECT saved_site_id FROM feed_items WHERE id = ?", items["Episode 1"]["id"]
    ) == [(None,)]

    # Deleting the feed removes its items; saved resources stay.
    assert env.client.delete(f"/api/feeds/{feed['id']}", headers=ORIGIN).status_code == 204
    assert env.query("SELECT count(*) FROM feed_items") == [(0,)]
    assert env.client.get(f"/api/library/sites/{existing['id']}").status_code == 200


def test_feeds_are_account_scoped_and_requests_strict(environment: Environment) -> None:
    env = environment
    _use(env.client, _register(env.client, "owner"))
    url = "https://mine.example/rss"
    env.fetcher.queue(
        url, _ok(rss([{"title": "Mine", "link": "https://mine.example/1", "guid": "1"}]))
    )
    feed = _add(env, url)
    item = _items(env)[0]

    assert env.client.post("/api/feeds", json={"url": "ftp://x"}, headers=ORIGIN).status_code == 422
    assert (
        env.client.post(
            "/api/feeds", json={"url": "https://a.example", "x": 1}, headers=ORIGIN
        ).status_code
        == 422
    )
    assert env.client.post("/api/feeds", json={"url": "https://a.example/rss"}).status_code == 403

    _use(env.client, _register(env.client, "intruder"))
    assert env.client.get("/api/feeds").json() == {"items": []}
    assert _items(env) == []
    assert env.client.post(f"/api/feeds/{feed['id']}/refresh", headers=ORIGIN).status_code == 404
    assert env.client.delete(f"/api/feeds/{feed['id']}", headers=ORIGIN).status_code == 404
    assert env.client.post(f"/api/feeds/items/{item['id']}/save", headers=ORIGIN).status_code == 404
    assert (
        env.client.patch(
            f"/api/feeds/items/{item['id']}", json={"read": True}, headers=ORIGIN
        ).status_code
        == 404
    )
    # The other account may subscribe to the same URL independently.
    env.fetcher.queue(url, _ok(rss([{"title": "Mine", "guid": "1"}])))
    assert _add(env, url)["item_count"] == 1

    env.client.cookies.clear()
    assert env.client.get("/api/feeds").status_code == 401


def test_mcp_get_feed_digest(environment: Environment) -> None:
    env = environment
    _use(env.client, _register(env.client, "agent-owner"))
    url = "https://digest.example/rss"
    env.fetcher.queue(
        url,
        _ok(rss([{"title": "Digest item", "link": "https://digest.example/1", "guid": "1"}])),
    )
    _add(env, url)
    token = env.client.post(
        "/api/agent-tokens", json={"name": "Hermes", "scopes": ["read"]}, headers=ORIGIN
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
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert not result.get("isError"), result
    payload = json.loads(result["content"][0]["text"])
    assert [item["title"] for item in payload["items"]] == ["Digest item"]
    assert payload["items"][0]["source"] == "Example Blog"
    assert payload["items"][0]["saved_to_library"] is False


# ------------------------------------------------- adversarial regressions


def test_parser_does_not_expand_entities_or_read_local_files() -> None:
    levels = "".join(
        f'<!ENTITY l{n} "' + f"&l{n - 1};" * 10 + '">' for n in range(1, 10)
    )
    bomb = (
        '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY l0 "lol">' + levels + "]>"
        '<rss version="2.0"><channel><title>&l9;</title>'
        "<item><title>t &l9;</title><guid>g</guid></item></channel></rss>"
    ).encode()
    parsed = parse_feed(bomb)
    assert len(parsed.title) < 100
    assert all(len(item.title) < 100 for item in parsed.items)

    xxe = (
        b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
        b'<rss version="2.0"><channel><title>&x;</title>'
        b"<item><title>a&x;</title><guid>g</guid></item></channel></rss>"
    )
    parsed = parse_feed(xxe)
    assert "root:" not in parsed.title
    assert all("root:" not in item.title for item in parsed.items)


def test_every_redirect_hop_is_revalidated(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_stream(
        monkeypatch,
        {
            "https://f.example/start": FakeResponse(302, {"location": "http://10.0.0.5/admin"}),
            "http://10.0.0.5/admin": ProviderTargetError("resource_private", "private"),
        },
    )
    result = asyncio.run(feed_fetch.fetch_feed("https://f.example/start", None, None))
    assert result.status == "error" and result.error == "unsafe_target"


def test_fetch_has_a_total_time_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    class Dripping(FakeResponse):
        async def aiter_bytes(self) -> AsyncIterator[bytes]:
            while True:
                await asyncio.sleep(0.01)
                yield b"x"

    _install_stream(monkeypatch, {"https://f.example/slow": Dripping(200, {})})
    monkeypatch.setattr(feed_fetch, "TOTAL_FETCH_SECONDS", 0.1)
    result = asyncio.run(feed_fetch.fetch_feed("https://f.example/slow", None, None))
    assert result.status == "error" and result.error == "network_error"


def test_one_failing_feed_does_not_stop_the_poll_and_refresh_is_not_reentrant(
    environment: Environment,
) -> None:
    env = environment
    _use(env.client, _register(env.client, "robust"))
    first = _add(env, "https://one.example/rss")
    second = _add(env, "https://two.example/rss")
    env.execute("UPDATE feeds SET next_fetch_at = ?", (NOW - timedelta(hours=1)).isoformat(" "))

    seen: list[str] = []

    async def exploding(url: str, etag: str | None, last_modified: str | None) -> FeedFetchResult:
        seen.append(url)
        if url == "https://one.example/rss":
            raise RuntimeError("boom")
        return _ok(rss([{"title": "Fine", "guid": "g"}]))

    database = env.database()
    try:
        assert asyncio.run(feed_service.poll_due_feeds(database, fetcher=exploding)) == 2
        assert sorted(seen) == ["https://one.example/rss", "https://two.example/rss"]

        async def reentrant() -> tuple[str, str]:
            gate = asyncio.Event()

            async def slow(*_args: object) -> FeedFetchResult:
                await gate.wait()
                return _ok(rss([{"title": "Slow", "guid": "s"}]))

            running = asyncio.create_task(
                feed_service.refresh_feed(database, first["id"], fetcher=slow)
            )
            await asyncio.sleep(0.05)
            second_call = await feed_service.refresh_feed(database, first["id"], fetcher=slow)
            gate.set()
            return (await running).status, second_call.status

        assert asyncio.run(reentrant()) == ("ok", "busy")
    finally:
        asyncio.run(database.dispose())
    assert second["id"]
