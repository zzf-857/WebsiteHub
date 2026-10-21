"""Q42 收藏模型 API：资源类型、使用信号、重温与概览。"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from webhub.bookmarks import persistence
from webhub.bookmarks.models import ParsedBookmark, ParsedFolder
from webhub.config import Settings
from webhub.db.database import Database
from webhub.db.migrations import upgrade_database
from webhub.db.models import Category, Site
from webhub.ingestion.worker import AnalysisSchedule
from webhub.library import service
from webhub.library.service.engagement import rediscover_pivot
from webhub.main import create_app

COOKIE_NAME = "webhub_session"
ORIGIN = {"Origin": "http://testserver"}
SITE_FIELDS = {
    "kind",
    "kind_source",
    "open_count",
    "last_opened_at",
    "seen_at",
    "starred",
    "starred_at",
    "resurface_muted",
    "meta",
}


@dataclass(frozen=True, slots=True)
class Environment:
    client: TestClient
    database_path: Path
    database_url: str


@pytest.fixture(autouse=True)
def _no_background_analysis(monkeypatch: pytest.MonkeyPatch) -> None:
    # Saves and list reads schedule page analysis; it would race the version
    # assertions below and needs the network. Not under test here.
    monkeypatch.setattr(
        "webhub.ingestion.worker.schedule_analysis",
        lambda *_args, **_kwargs: AnalysisSchedule(queued=0, already_queued=0, rejected=0),
    )
    monkeypatch.setattr(
        "webhub.ingestion.worker.ensure_auto_backfill",
        lambda *_args, **_kwargs: False,
    )


@pytest.fixture
def environment(tmp_path: Path) -> Iterator[Environment]:
    database_path = tmp_path / "main.sqlite3"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    settings = Settings(environment="test", database_url=database_url, data_directory=tmp_path)
    upgrade_database(settings.database_url)
    with TestClient(create_app(settings=settings)) as client:
        yield Environment(client, database_path, database_url)


def _register(client: TestClient, username: str) -> tuple[str, str]:
    response = client.post(
        "/api/auth/register",
        json={"username": username, "password": "a sufficiently secure password"},
        headers=ORIGIN,
    )
    assert response.status_code == 201, response.text
    token = response.cookies.get(COOKIE_NAME)
    assert token
    return str(response.json()["user"]["id"]), token


def _use_token(client: TestClient, token: str) -> None:
    client.cookies.clear()
    client.cookies.set(COOKIE_NAME, token)


def _tag(client: TestClient, name: str) -> str:
    response = client.post("/api/library/tags", json={"name": name}, headers=ORIGIN)
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _site(
    client: TestClient,
    url: str,
    *,
    name: str | None = None,
    tag_ids: list[str] | None = None,
    source: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {"name": name or url[:80], "url": url, "tag_ids": tag_ids or []}
    if source is not None:
        payload["source"] = source
    response = client.post("/api/library/sites", json=payload, headers=ORIGIN)
    assert response.status_code == 201, response.text
    return response.json()


def _open(client: TestClient, site_id: object) -> dict[str, object]:
    response = client.post(f"/api/library/sites/{site_id}/open", headers=ORIGIN)
    assert response.status_code == 200, response.text
    return response.json()


def _state(client: TestClient, site_id: object, **payload: object) -> dict[str, object]:
    response = client.put(f"/api/library/sites/{site_id}/state", json=payload, headers=ORIGIN)
    assert response.status_code == 200, response.text
    return response.json()


def _stamp(value: datetime) -> str:
    """SQLAlchemy's SQLite storage format for UTC ``DateTime`` columns."""

    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")


def _set(database_path: Path, site_id: object, **columns: object) -> None:
    assignments = ", ".join(f"{name} = ?" for name in columns)
    with sqlite3.connect(database_path) as connection:
        cursor = connection.execute(
            f"UPDATE sites SET {assignments} WHERE id = ?",
            (*columns.values(), str(site_id)),
        )
        assert cursor.rowcount == 1
        connection.commit()


def _row(database_path: Path, site_id: object, *columns: str) -> tuple[object, ...]:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            f"SELECT {', '.join(columns)} FROM sites WHERE id = ?",
            (str(site_id),),
        ).fetchone()
    assert row is not None
    return tuple(row)


def _all_site_rows(database_path: Path) -> list[tuple[object, ...]]:
    with sqlite3.connect(database_path) as connection:
        return connection.execute("SELECT * FROM sites ORDER BY id").fetchall()


def _ids(payload: dict[str, object]) -> list[str]:
    return [str(item["id"]) for item in payload["items"]]  # type: ignore[union-attr,index]


# ── 资源类型：所有建站路径都推断 ─────────────────────────────────────────


def test_rest_agent_and_batch_creation_infer_the_kind(environment: Environment) -> None:
    client = environment.client
    _register(client, "alice")

    repo = _site(client, "https://github.com/langchain-ai/langgraph")
    assert set(repo) >= SITE_FIELDS
    assert {key: repo[key] for key in SITE_FIELDS} == {
        "kind": "repo",
        "kind_source": "auto",
        "open_count": 0,
        "last_opened_at": None,
        "seen_at": None,
        "starred": False,
        "starred_at": None,
        "resurface_muted": False,
        "meta": {"host": "github.com", "owner": "langchain-ai", "repo": "langgraph"},
    }
    agent_video = _site(client, "https://www.youtube.com/watch?v=dQw4w9WgXcQ", source="agent")
    assert (agent_video["source"], agent_video["kind"], agent_video["meta"]) == (
        "agent",
        "video",
        {},
    )

    batch = client.post(
        "/api/library/sites/batch",
        json={
            "urls": [
                "https://huggingface.co/datasets/openai/gsm8k",
                "https://zhuanlan.zhihu.com/p/123456",
                "https://tool.example/",
            ],
            "confirm": True,
            "source": "agent",
        },
        headers=ORIGIN,
    )
    assert batch.status_code == 200, batch.text
    assert batch.json()["created"] == 3
    created = {
        str(item["url"]): str(item["site_id"]) for item in batch.json()["items"]
    }
    kinds = {
        url: client.get(f"/api/library/sites/{site_id}").json()
        for url, site_id in created.items()
    }
    assert kinds["https://huggingface.co/datasets/openai/gsm8k"]["kind"] == "repo"
    assert kinds["https://huggingface.co/datasets/openai/gsm8k"]["meta"] == {
        "host": "huggingface.co",
        "owner": "openai",
        "repo": "gsm8k",
        "repo_type": "dataset",
    }
    assert kinds["https://zhuanlan.zhihu.com/p/123456"]["kind"] == "article"
    assert kinds["https://tool.example/"]["kind"] == "website"

    listed = client.get("/api/library/sites", params={"limit": 100}).json()["items"]
    assert all(set(item) >= SITE_FIELDS for item in listed)


def test_any_orm_insert_into_sites_infers_kind_per_row(environment: Environment) -> None:
    """The column default is the guarantee for every present and future insert path."""

    client = environment.client
    user_id, _ = _register(client, "alice")
    database = Database(environment.database_url)

    async def scenario() -> dict[str, tuple[str, str, str]]:
        async with database.sessions() as session:
            category = await session.scalar(
                select(Category).where(Category.user_id == user_id, Category.is_default)
            )
            assert category is not None
            urls = (
                "https://gitlab.com/group/sub/project",
                "https://b23.tv/AbCdEf",
                "https://example.com/blog/a-post",
                "https://example.com/",
            )
            session.add_all(
                Site(
                    user_id=user_id,
                    category_id=category.id,
                    position=100 + index,
                    name=f"ORM {index}",
                    normalized_name=f"orm {index}",
                    original_url=url,
                    identity_url=url,
                )
                for index, url in enumerate(urls)
            )
            await session.commit()
            rows = (
                await session.execute(
                    select(Site.identity_url, Site.kind, Site.kind_source, Site.meta_json).where(
                        Site.user_id == user_id
                    )
                )
            ).all()
        return {str(url): (kind, source, meta) for url, kind, source, meta in rows}

    try:
        stored = asyncio.run(scenario())
    finally:
        asyncio.run(database.dispose())
    assert stored == {
        "https://gitlab.com/group/sub/project": (
            "repo",
            "auto",
            '{"host":"gitlab.com","owner":"group/sub","repo":"project"}',
        ),
        "https://b23.tv/AbCdEf": ("video", "auto", "{}"),
        "https://example.com/blog/a-post": ("article", "auto", "{}"),
        "https://example.com/": ("website", "auto", "{}"),
    }


def test_bookmark_import_apply_infers_the_kind(environment: Environment) -> None:
    client = environment.client
    user_id, _ = _register(client, "alice")
    urls = {
        "https://github.com/owner/bookmarked-repo": "repo",
        "https://www.youtube.com/watch?v=abc123": "video",
        "https://blog.example/posts/hello-world": "article",
        "https://plain.example/": "website",
    }
    events: list[ParsedFolder | ParsedBookmark] = [
        ParsedFolder(
            source_folder_id=1,
            parent_source_folder_id=None,
            source_order=1,
            source_sequence=1,
            title="收藏",
            folder_path=("收藏",),
            depth=1,
        ),
        *(
            ParsedBookmark(
                position=index + 1,
                source_sequence=index + 2,
                raw_url=url,
                title=f"Bookmark {index}",
                folder_path=("收藏",),
                source_folder_id=1,
                add_date=None,
                last_modified=None,
            )
            for index, url in enumerate(urls)
        ),
    ]
    source_sha256 = hashlib.sha256(b"q42-kinds").hexdigest()
    database = Database(environment.database_url)

    async def import_job() -> str:
        async with database.sessions() as session:
            created = await persistence.create_import(
                session,
                user_id,
                source_sha256=source_sha256,
                source_size_bytes=1_024,
                original_filename="kinds.html",
                idempotency_key="upload-q42-kinds-request",
            )
        async with database.sessions() as session:
            run = await persistence.begin_parse_run(
                session,
                user_id,
                created.job_id,
                expected_job_version=1,
                idempotency_key="parse-q42-kinds-request",
            )
        async with database.sessions() as session:
            await persistence.append_parse_chunk(
                session, user_id, created.job_id, run.run_id, chunk_index=0, events=events
            )
        async with database.sessions() as session:
            await persistence.finalize_parse_run(
                session,
                user_id,
                created.job_id,
                run.run_id,
                expected_job_version=2,
                completion=persistence.ParseCompletion(
                    source_sha256=source_sha256,
                    source_sequence_count=len(events),
                    folder_count=1,
                    occurrence_count=len(urls),
                ),
            )
        return created.job_id

    try:
        job_id = asyncio.run(import_job())
    finally:
        asyncio.run(database.dispose())
    summary = client.get(f"/api/bookmark-imports/{job_id}/preview")
    assert summary.status_code == 200, summary.text
    applied = client.post(
        f"/api/bookmark-imports/{job_id}/apply",
        json={
            "expected_job_version": summary.json()["job_version"],
            "expected_decision_version": summary.json()["decision_version"],
        },
        headers=ORIGIN,
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["created"] == len(urls)

    items = client.get("/api/library/sites", params={"limit": 100}).json()["items"]
    assert {item["identity_url"]: item["kind"] for item in items} == urls
    assert {item["source"] for item in items} == {"browser_import"}
    repo = next(item for item in items if item["kind"] == "repo")
    assert repo["meta"] == {"host": "github.com", "owner": "owner", "repo": "bookmarked-repo"}


def test_url_edits_reinfer_auto_kinds_and_a_kind_patch_is_manual(
    environment: Environment,
) -> None:
    client = environment.client
    _register(client, "alice")
    site = _site(client, "https://example.com/")
    assert (site["kind"], site["kind_source"]) == ("website", "auto")

    def patch(**payload: object) -> dict[str, object]:
        response = client.patch(
            f"/api/library/sites/{site['id']}",
            json={"expected_version": site["version"], **payload},
            headers=ORIGIN,
        )
        assert response.status_code == 200, response.text
        return response.json()

    site = patch(url="https://github.com/owner/repo")
    assert (site["kind"], site["kind_source"], site["version"]) == ("repo", "auto", 2)
    assert site["meta"] == {"host": "github.com", "owner": "owner", "repo": "repo"}

    site = patch(kind="article")
    assert (site["kind"], site["kind_source"], site["meta"]) == ("article", "manual", {})

    # Manual kinds survive later URL edits.
    site = patch(url="https://www.youtube.com/watch?v=abc")
    assert (site["kind"], site["kind_source"]) == ("article", "manual")

    # Repeating the inferred value is still an explicit decision.
    site = patch(kind="repo")
    assert (site["kind"], site["kind_source"], site["meta"]) == ("repo", "manual", {})
    site = patch(url="https://gitee.com/owner/project")
    assert (site["kind"], site["kind_source"]) == ("repo", "manual")
    assert site["meta"] == {"host": "gitee.com", "owner": "owner", "repo": "project"}

    for invalid in ({"kind": None}, {"kind": "podcast"}, {"kind_source": "auto"}):
        response = client.patch(
            f"/api/library/sites/{site['id']}",
            json={"expected_version": site["version"], **invalid},
            headers=ORIGIN,
        )
        assert response.status_code == 422, (invalid, response.text)
    unchanged = client.get(f"/api/library/sites/{site['id']}").json()
    assert (unchanged["version"], unchanged["kind"], unchanged["kind_source"]) == (
        site["version"],
        "repo",
        "manual",
    )


# ── 打开记录与阅读状态：不改 version / updated_at ──────────────────────────

FROZEN_UPDATED_AT = "2026-01-02 03:04:05.000000"


def test_open_counts_without_touching_version_or_updated_at(environment: Environment) -> None:
    client = environment.client
    _register(client, "alice")
    site = _site(client, "https://github.com/owner/repo")
    # A stored value far in the past: if the ORM ``onupdate=utc_now`` hook
    # fired on the usage UPDATE, it would be replaced by "now".
    _set(environment.database_path, site["id"], updated_at=FROZEN_UPDATED_AT)

    first = _open(client, site["id"])
    assert set(first) == {"site_id", "open_count", "last_opened_at", "seen_at"}
    assert first["site_id"] == site["id"]
    assert first["open_count"] == 1
    assert first["seen_at"] == first["last_opened_at"]

    second = _open(client, site["id"])
    assert second["open_count"] == 2
    assert second["seen_at"] == first["seen_at"]
    assert str(second["last_opened_at"]) >= str(first["last_opened_at"])

    assert _row(environment.database_path, site["id"], "version", "updated_at") == (
        1,
        FROZEN_UPDATED_AT,
    )
    detail = client.get(f"/api/library/sites/{site['id']}").json()
    assert (detail["version"], detail["open_count"]) == (1, 2)
    assert detail["last_opened_at"] == second["last_opened_at"]
    assert detail["seen_at"] == first["seen_at"]

    # An edit form opened before those clicks still saves without a conflict,
    # and an edit (unlike usage) does move updated_at: the check above is live.
    edited = client.patch(
        f"/api/library/sites/{site['id']}",
        json={"expected_version": 1, "name": "Renamed"},
        headers=ORIGIN,
    )
    assert edited.status_code == 200, edited.text
    version, updated_at = _row(environment.database_path, site["id"], "version", "updated_at")
    assert version == 2
    assert updated_at != FROZEN_UPDATED_AT


def test_state_star_seen_snooze_and_mute_are_reversible_and_version_free(
    environment: Environment,
) -> None:
    client = environment.client
    _register(client, "alice")
    site = _site(client, "https://example.com/blog/post-1")
    site_id = site["id"]
    _set(environment.database_path, site_id, updated_at=FROZEN_UPDATED_AT)

    starred = _state(client, site_id, starred=True)
    assert starred["starred"] is True
    assert starred["starred_at"] is not None
    assert set(starred) >= SITE_FIELDS | {"id", "version", "updated_at"}
    again = _state(client, site_id, starred=True)
    assert again["starred_at"] == starred["starred_at"]
    unstarred = _state(client, site_id, starred=False)
    assert (unstarred["starred"], unstarred["starred_at"]) == (False, None)

    seen = _state(client, site_id, seen=True)
    assert seen["seen_at"] is not None
    assert seen["open_count"] == 0
    assert _state(client, site_id, seen=True)["seen_at"] == seen["seen_at"]
    assert _state(client, site_id, seen=False)["seen_at"] is None

    before = datetime.now(UTC)
    _state(client, site_id, snooze_days=7)
    due_at, count = _row(environment.database_path, site_id, "resurface_due_at", "resurface_count")
    due = datetime.fromisoformat(str(due_at)).replace(tzinfo=UTC)
    assert before + timedelta(days=7) - timedelta(seconds=1) <= due
    assert due <= datetime.now(UTC) + timedelta(days=7)
    assert count == 1
    _state(client, site_id, snooze_days=90)
    assert _row(environment.database_path, site_id, "resurface_count") == (2,)
    _state(client, site_id, snooze_days=0)
    assert _row(environment.database_path, site_id, "resurface_due_at", "resurface_count") == (
        None,
        2,
    )

    muted = _state(client, site_id, resurface_muted=True, starred=True)
    assert (muted["resurface_muted"], muted["starred"]) == (True, True)
    assert _state(client, site_id, resurface_muted=False)["resurface_muted"] is False

    final = client.get(f"/api/library/sites/{site_id}").json()
    assert final["version"] == 1
    assert _row(environment.database_path, site_id, "version", "updated_at") == (
        1,
        FROZEN_UPDATED_AT,
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"starred": None},
        {"seen": None, "starred": True},
        {"starred": "true"},
        {"starred": 1},
        {"seen": "yes"},
        {"resurface_muted": 0},
        {"snooze_days": 91},
        {"snooze_days": -1},
        {"snooze_days": 7.5},
        {"snooze_days": "7"},
        {"snooze_days": True},
        {"pinned": True},
        {"starred": True, "version": 3},
    ],
)
def test_state_request_body_is_strict(environment: Environment, payload: object) -> None:
    client = environment.client
    _register(client, "alice")
    site = _site(client, "https://example.com/")
    before = _all_site_rows(environment.database_path)
    response = client.put(f"/api/library/sites/{site['id']}/state", json=payload, headers=ORIGIN)
    assert response.status_code == 422, response.text
    assert _all_site_rows(environment.database_path) == before


def test_engagement_requires_login_trusted_origin_and_ownership(
    environment: Environment,
) -> None:
    client = environment.client
    _, alice = _register(client, "alice")
    site = _site(client, "https://example.com/")
    _, bob = _register(client, "bob")
    bob_site = _site(client, "https://bob.example/")
    _use_token(client, alice)
    before = _all_site_rows(environment.database_path)

    open_path = f"/api/library/sites/{site['id']}/open"
    state_path = f"/api/library/sites/{site['id']}/state"
    for headers in ({}, {"Origin": "http://attacker.invalid"}):
        assert client.post(open_path, headers=headers).status_code == 403
        assert client.put(state_path, json={"starred": True}, headers=headers).status_code == 403

    for missing in (bob_site["id"], "00000000-0000-0000-0000-000000000000"):
        assert client.post(f"/api/library/sites/{missing}/open", headers=ORIGIN).status_code == 404
        not_found = client.put(
            f"/api/library/sites/{missing}/state", json={"starred": True}, headers=ORIGIN
        )
        assert not_found.status_code == 404
        assert not_found.json()["detail"]["code"] == "not_found"
    assert _all_site_rows(environment.database_path) == before

    _use_token(client, bob)
    assert client.post(open_path, headers=ORIGIN).status_code == 404
    assert client.put(state_path, json={"seen": True}, headers=ORIGIN).status_code == 404
    assert _all_site_rows(environment.database_path) == before

    client.cookies.clear()
    assert client.post(open_path, headers=ORIGIN).status_code == 401
    assert client.put(state_path, json={"starred": True}, headers=ORIGIN).status_code == 401
    assert client.get("/api/discover/rediscover").status_code == 401
    assert client.get("/api/library/overview").status_code == 401


# ── 重温 ─────────────────────────────────────────────────────────────────


def test_rediscover_candidates_order_reasons_and_purity(environment: Environment) -> None:
    client = environment.client
    alice_id, alice = _register(client, "alice")
    path = environment.database_path
    now = datetime.now(UTC)
    agent = _tag(client, "Agent")
    other = _tag(client, "其他")

    def saved(url: str, days: int, tags: list[str] | None = None) -> str:
        site_id = str(_site(client, url, tag_ids=tags)["id"])
        _set(path, site_id, created_at=_stamp(now - timedelta(days=days)))
        return site_id

    old_agent = saved("https://github.com/someone/old-agent", 43, [agent])
    old_plain = saved("https://plain.example/", 10)
    old_other = saved("https://other.example/blog/notes", 5, [other])
    snoozed_due = saved("https://due.example/", 20)
    _state(client, snoozed_due, snooze_days=7)
    _set(path, snoozed_due, resurface_due_at=_stamp(now - timedelta(hours=1)))
    # Recent saves make "Agent" a current interest (two uses in 30 days).
    saved("https://too-new.example/", 2, [agent])
    saved("https://recent.example/", 1, [agent])
    _open(client, saved("https://opened.example/", 30, [agent]))
    _state(client, saved("https://seen.example/", 30, [agent]), seen=True)
    _state(client, saved("https://muted.example/", 30, [agent]), resurface_muted=True)
    _state(client, saved("https://snoozed.example/", 30, [agent]), snooze_days=7)

    _, bob = _register(client, "bob")
    bob_site = str(_site(client, "https://bob.example/")["id"])
    _set(path, bob_site, created_at=_stamp(now - timedelta(days=40)))
    _use_token(client, alice)

    before = _all_site_rows(path)
    response = client.get("/api/discover/rediscover", params={"limit": 12})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == {"generated_for", "items"}
    generated_for = date.fromisoformat(payload["generated_for"])
    assert generated_for in {now.date(), datetime.now(UTC).date()}
    items = payload["items"]
    assert all(set(item) == {"site", "saved_days", "reason"} for item in items)
    picked = [str(item["site"]["id"]) for item in items]
    assert sorted(picked) == sorted([old_agent, old_plain, old_other, snoozed_due])

    # Tag overlap first, then the deterministic id rotation for that day.
    assert picked[0] == old_agent
    pivot = rediscover_pivot(alice_id, generated_for, 0)
    rest = sorted([old_plain, old_other, snoozed_due])
    assert picked[1:] == [i for i in rest if i >= pivot] + [i for i in rest if i < pivot]

    by_id = {str(item["site"]["id"]): item for item in items}
    assert by_id[old_agent]["saved_days"] == 43
    assert by_id[old_agent]["reason"] == (
        "43 天前收藏，一次都没打开过；和你最近常收藏的「Agent」相关"
    )
    assert by_id[old_plain]["reason"] == "10 天前收藏，一次都没打开过"
    assert by_id[snoozed_due]["reason"] == "20 天前收藏，一次都没打开过；推迟过 1 次，提醒已到期"
    assert by_id[old_agent]["site"]["kind"] == "repo"

    # Same day, same shuffle: identical answer. And GET writes nothing.
    assert client.get("/api/discover/rediscover", params={"limit": 12}).json() == payload
    short = client.get("/api/discover/rediscover", params={"limit": 2}).json()
    assert [item["site"]["id"] for item in short["items"]] == picked[:2]
    default = client.get("/api/discover/rediscover").json()
    assert len(default["items"]) == 4
    assert _all_site_rows(path) == before

    for params in ({"limit": 13}, {"limit": 0}, {"shuffle": -1}, {"shuffle": "x"}):
        assert client.get("/api/discover/rediscover", params=params).status_code == 422

    _use_token(client, bob)
    bob_items = client.get("/api/discover/rediscover").json()["items"]
    assert [item["site"]["id"] for item in bob_items] == [bob_site]


def test_rediscover_rotation_is_stable_per_utc_day_and_shuffle(
    environment: Environment,
) -> None:
    client = environment.client
    user_id, _ = _register(client, "alice")
    now = datetime(2026, 10, 21, 1, 0, tzinfo=UTC)
    site_ids = sorted(
        str(_site(client, f"https://rotation-{index}.example/")["id"]) for index in range(12)
    )
    for site_id in site_ids:
        _set(environment.database_path, site_id, created_at=_stamp(now - timedelta(days=9)))
    database = Database(environment.database_url)

    async def order(at: datetime, shuffle: int, limit: int = 12) -> list[str]:
        async with database.sessions() as session:
            result = await service.rediscover(
                session, user_id, limit=limit, shuffle=shuffle, now=at
            )
        assert result.generated_for == at.date()
        return [item.site.id for item in result.items]

    def rotation(day: date, shuffle: int) -> list[str]:
        pivot = rediscover_pivot(user_id, day, shuffle)
        return [i for i in site_ids if i >= pivot] + [i for i in site_ids if i < pivot]

    async def scenario() -> None:
        morning = await order(now, 0)
        assert morning == rotation(now.date(), 0)
        assert await order(now.replace(hour=23, minute=59), 0) == morning
        assert await order(now, 0, limit=4) == morning[:4]
        tomorrow = now + timedelta(days=1)
        assert await order(tomorrow, 0) == rotation(tomorrow.date(), 0)
        shuffled = {tuple(await order(now, shuffle, limit=4)) for shuffle in range(10)}
        assert len(shuffled) > 1
        for shuffle in range(10):
            assert await order(now, shuffle) == rotation(now.date(), shuffle)

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(database.dispose())


def test_rediscover_ranks_by_interest_overlap_count_and_cards_keep_their_tags(
    environment: Environment,
) -> None:
    client = environment.client
    _register(client, "alice")
    path = environment.database_path
    now = datetime.now(UTC)
    agent, rag, misc = _tag(client, "Agent"), _tag(client, "RAG"), _tag(client, "杂项")

    def saved(url: str, days: int, tags: list[str]) -> str:
        site_id = str(_site(client, url, tag_ids=tags)["id"])
        _set(path, site_id, created_at=_stamp(now - timedelta(days=days)))
        return site_id

    # Recent saves (too new to resurface) make Agent and RAG current interests;
    # 杂项 is on one save of the last 30 days only, which is not yet "常收藏".
    for index in range(2):
        saved(f"https://interest-{index}.example/", 1, [agent, rag])
    both = saved("https://both.example/", 12, [rag, agent, misc])
    only_rag = saved("https://only-rag.example/", 15, [rag])
    only_misc = saved("https://only-misc.example/", 40, [misc])
    untagged = saved("https://untagged.example/", 25, [])

    items = client.get("/api/discover/rediscover", params={"limit": 12}).json()["items"]
    picked = [str(item["site"]["id"]) for item in items]
    assert picked[:2] == [both, only_rag]
    assert sorted(picked[2:]) == sorted([only_misc, untagged])

    by_id = {str(item["site"]["id"]): item for item in items}
    # Each card carries its own tags (loaded in one batch for all picks).
    assert {tag["name"] for tag in by_id[both]["site"]["tags"]} == {"Agent", "RAG", "杂项"}
    assert [tag["name"] for tag in by_id[only_misc]["site"]["tags"]] == ["杂项"]
    assert by_id[untagged]["site"]["tags"] == []
    assert all(item["site"]["category"]["is_default"] for item in items)
    # Reasons quote matched interests by recent use: RAG is on four saves of
    # the last 30 days (both, only_rag and the two fresh ones), Agent on three.
    assert by_id[both]["reason"] == (
        "12 天前收藏，一次都没打开过；和你最近常收藏的「RAG」「Agent」相关"
    )
    assert by_id[only_rag]["reason"] == "15 天前收藏，一次都没打开过；和你最近常收藏的「RAG」相关"
    assert by_id[only_misc]["reason"] == "40 天前收藏，一次都没打开过"


# ── 概览 ─────────────────────────────────────────────────────────────────


def test_overview_counts_are_account_scoped(environment: Environment) -> None:
    client = environment.client
    _, alice = _register(client, "alice")
    path = environment.database_path
    old = _stamp(datetime.now(UTC) - timedelta(days=10))

    repo_starred = _site(client, "https://github.com/a/one")
    _state(client, repo_starred["id"], starred=True)
    repo_opened_long_ago = _site(client, "https://github.com/a/two")
    _open(client, repo_opened_long_ago["id"])
    _set(path, repo_opened_long_ago["id"], created_at=old, last_opened_at=old)
    _open(client, _site(client, "https://youtu.be/abc")["id"])
    _state(client, _site(client, "https://example.com/blog/post")["id"], seen=True)
    _state(client, _site(client, "https://example.org/")["id"], starred=True)
    _site(client, "https://example.net/")

    overview = client.get("/api/library/overview")
    assert overview.status_code == 200, overview.text
    assert overview.json() == {
        "total": 6,
        "by_kind": {"repo": 2, "article": 1, "video": 1, "website": 2},
        # Never opened and not marked seen: repo_starred, example.org, example.net.
        "unopened": 3,
        "starred": 2,
        "added_7d": 5,
        "opened_7d": 1,
    }

    _, bob = _register(client, "bob")
    assert client.get("/api/library/overview").json() == {
        "total": 0,
        "by_kind": {"repo": 0, "article": 0, "video": 0, "website": 0},
        "unopened": 0,
        "starred": 0,
        "added_7d": 0,
        "opened_7d": 0,
    }
    _site(client, "https://bob.example/")
    assert client.get("/api/library/overview").json()["total"] == 1
    _use_token(client, alice)
    assert client.get("/api/library/overview").json()["total"] == 6


# ── 列表筛选、opened 排序与游标 ────────────────────────────────────────────


def test_list_kind_and_state_filters_opened_sort_and_cursor_scope(
    environment: Environment,
) -> None:
    client = environment.client
    _register(client, "alice")
    path = environment.database_path
    base = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    sites = {
        "r1": _site(client, "https://github.com/a/r1")["id"],
        "r2": _site(client, "https://github.com/a/r2")["id"],
        "v1": _site(client, "https://youtu.be/v1")["id"],
        "a1": _site(client, "https://example.com/blog/a1")["id"],
        "w1": _site(client, "https://w1.example/")["id"],
        "w2": _site(client, "https://w2.example/")["id"],
    }
    for name, hours in (("r1", 3), ("v1", 1), ("a1", 2)):
        _open(client, sites[name])
        stamp = _stamp(base - timedelta(hours=hours))
        _set(path, sites[name], last_opened_at=stamp, seen_at=stamp)
    _state(client, sites["w1"], starred=True)
    _state(client, sites["w2"], seen=True)

    def listed(**params: object) -> dict[str, object]:
        response = client.get("/api/library/sites", params={"limit": 100, **params})
        assert response.status_code == 200, response.text
        return response.json()

    def names(payload: dict[str, object]) -> set[str]:
        lookup = {str(site_id): name for name, site_id in sites.items()}
        return {lookup[site_id] for site_id in _ids(payload)}

    repos = listed(kind="repo")
    assert names(repos) == {"r1", "r2"}
    assert repos["aggregate"]["matched_count"] == 2  # type: ignore[index]
    assert names(listed(kind="video")) == {"v1"}
    assert names(listed(state="opened")) == {"r1", "v1", "a1"}
    assert names(listed(state="unopened")) == {"r2", "w1"}
    assert names(listed(state="starred")) == {"w1"}
    assert names(listed(kind="repo", state="unopened")) == {"r2"}
    for invalid in ({"kind": "podcast"}, {"state": "archived"}, {"sort": "opened_at"}):
        assert client.get("/api/library/sites", params=invalid).status_code == 422

    selection = client.get("/api/library/sites/selection", params={"kind": "repo"})
    assert {item["id"] for item in selection.json()["items"]} == {sites["r1"], sites["r2"]}
    starred_selection = client.get("/api/library/sites/selection", params={"state": "starred"})
    assert [item["id"] for item in starred_selection.json()["items"]] == [sites["w1"]]

    never_opened = sorted(str(sites[name]) for name in ("r2", "w1", "w2"))

    def walk(direction: str) -> list[str]:
        seen: list[str] = []
        cursor = None
        while True:
            params: dict[str, object] = {"sort": "opened", "direction": direction, "limit": 2}
            if cursor:
                params["cursor"] = cursor
            page = client.get("/api/library/sites", params=params)
            assert page.status_code == 200, page.text
            seen.extend(_ids(page.json()))
            cursor = page.json()["next_cursor"]
            if cursor is None:
                return seen

    # Never-opened rows trail in both directions (spec: 空值排后).
    opened_desc = [str(sites[name]) for name in ("v1", "a1", "r1")]
    assert walk("desc") == opened_desc + list(reversed(never_opened))
    assert walk("asc") == list(reversed(opened_desc)) + never_opened

    first_page = client.get(
        "/api/library/sites", params={"sort": "opened", "limit": 2, "kind": "repo"}
    ).json()
    assert first_page["next_cursor"] is None
    unfiltered = client.get("/api/library/sites", params={"sort": "opened", "limit": 2}).json()
    cursor = unfiltered["next_cursor"]
    assert cursor
    for changed in ({"kind": "repo"}, {"state": "opened"}, {"kind": "video", "state": "opened"}):
        response = client.get(
            "/api/library/sites",
            params={"sort": "opened", "limit": 2, "cursor": cursor, **changed},
        )
        assert response.status_code == 422, (changed, response.text)
    filtered = client.get(
        "/api/library/sites", params={"sort": "opened", "limit": 1, "state": "opened"}
    ).json()
    assert filtered["next_cursor"]
    leaked = client.get(
        "/api/library/sites",
        params={"sort": "opened", "limit": 1, "cursor": filtered["next_cursor"]},
    )
    assert leaked.status_code == 422

    # Another account sees none of these through any of the new filters, and
    # cannot page with this account's cursor.
    _register(client, "bob")
    for params in ({"kind": "repo"}, {"state": "starred"}, {"state": "opened"}, {}):
        assert listed(sort="opened", **params)["items"] == []
    foreign = client.get(
        "/api/library/sites", params={"sort": "opened", "limit": 2, "cursor": cursor}
    )
    assert foreign.status_code == 422


# ── 相似排查：冻结卡片显示实时类型与使用信号 ─────────────────────────────────


def test_similarity_members_show_live_kind_and_usage(environment: Environment) -> None:
    client = environment.client
    _register(client, "alice")
    first = _site(client, "https://dup.example/blog/post-1")
    second = _site(client, "http://www.dup.example/blog/post-1/?utm_source=test")
    assert (first["kind"], second["kind"]) == ("article", "article")
    retyped = client.patch(
        f"/api/library/sites/{first['id']}",
        json={"expected_version": 1, "kind": "video"},
        headers=ORIGIN,
    )
    assert retyped.status_code == 200, retyped.text
    _state(client, first["id"], starred=True)
    _open(client, second["id"])

    scan = client.post("/api/library/site-similarity-scans", headers=ORIGIN)
    assert scan.status_code in {200, 201}, scan.text
    run_id = scan.json()["id"]

    def members() -> dict[str, dict[str, object]]:
        page = client.get(f"/api/library/site-similarity-scans/{run_id}/groups")
        assert page.status_code == 200, page.text
        (group,) = page.json()["items"]
        return {str(member["id"]): member for member in group["members"]}

    live = members()
    # Live values, not re-derived from the frozen URL (which would say article).
    assert (live[first["id"]]["kind"], live[first["id"]]["kind_source"]) == ("video", "manual")
    assert live[first["id"]]["starred"] is True
    assert live[first["id"]]["version"] == 2
    assert live[second["id"]]["open_count"] == 1
    assert live[second["id"]]["starred"] is False

    deleted = client.delete(
        f"/api/library/sites/{first['id']}", params={"expected_version": 2}, headers=ORIGIN
    )
    assert deleted.status_code == 200, deleted.text
    frozen = members()[first["id"]]
    assert (frozen["kind"], frozen["kind_source"], frozen["starred"]) == ("article", "auto", False)
    assert (frozen["open_count"], frozen["meta"]) == (0, {})
