from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from webhub.config import Settings
from webhub.db.migrations import upgrade_database
from webhub.ingestion.worker import AnalysisSchedule
from webhub.library.batch import MAX_BATCH_URLS, extract_urls
from webhub.main import create_app

ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture(autouse=True)
def _disable_background_analysis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "webhub.library.routes.ingestion_worker.schedule_analysis",
        lambda *_args, **_kwargs: AnalysisSchedule(
            queued=0,
            already_queued=0,
            rejected=0,
        ),
    )


@contextmanager
def _client(tmp_path: Path) -> Iterator[tuple[TestClient, Path]]:
    database_path = tmp_path / "main.sqlite3"
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        data_directory=tmp_path,
        provider_master_key=b"provider-test-master-key-32bytes",
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
        yield client, database_path


def _site_count(database_path: Path) -> int:
    with sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM sites").fetchone()[0])


def test_urls_are_extracted_by_code_not_by_the_model() -> None:
    """The whole reason this module exists: a loop the model cannot skip."""

    text = """先看 https://example.com/a 和 https://example.com/b。
    括号里的（https://example.com/c）也算。
    维基的 https://en.wikipedia.org/wiki/Foo_(bar) 不能被截断。
    句子里的 (https://example.com/d) 收尾括号要去掉。
    重复的 https://example.com/a 只算一次。裸域名 example.com 不算。"""

    assert extract_urls(text) == [
        "https://example.com/a",
        "https://example.com/b",
        "https://example.com/c",
        "https://en.wikipedia.org/wiki/Foo_(bar)",
        "https://example.com/d",
    ]


def test_extraction_is_capped() -> None:
    text = " ".join(f"https://example.com/{index}" for index in range(MAX_BATCH_URLS + 20))
    assert len(extract_urls(text)) == MAX_BATCH_URLS


def test_a_mixed_batch_reports_per_item_status_without_writing(tmp_path: Path) -> None:
    """The queue's acceptance case: one duplicate, one invalid, none blocking."""

    with _client(tmp_path) as (client, database_path):
        existing = client.post(
            "/api/library/sites",
            json={"name": "已有", "url": "https://example.com/dup"},
            headers=ORIGIN,
        )
        assert existing.status_code == 201
        before = _site_count(database_path)

        preview = client.post(
            "/api/library/sites/batch",
            json={
                "urls": [
                    "https://example.com/one",
                    "https://example.com/dup",
                    "ftp://example.com/nope",
                    "https://example.com/two",
                    "https://EXAMPLE.com/one",
                ]
            },
            headers=ORIGIN,
        )
        assert preview.status_code == 200, preview.text
        body = preview.json()
        assert body["confirmed"] is False
        assert [item["status"] for item in body["items"]] == [
            "ready",
            "duplicate",
            "invalid",
            "ready",
            # Same address, different spelling: caught inside the batch itself.
            "duplicate",
        ]
        assert body["ready"] == 2
        # Preview is read-only by construction.
        assert _site_count(database_path) == before


def test_confirming_creates_only_the_importable_items(tmp_path: Path) -> None:
    with _client(tmp_path) as (client, database_path):
        payload = {
            "urls": [
                "https://example.com/one",
                "ftp://example.com/nope",
                "https://example.com/two",
            ],
            "confirm": True,
        }
        result = client.post("/api/library/sites/batch", json=payload, headers=ORIGIN)
        assert result.status_code == 200, result.text
        body = result.json()
        assert body["created"] == 2
        assert body["invalid"] == 1
        assert _site_count(database_path) == 2
        assert all(item["site_id"] for item in body["items"] if item["status"] == "created")


def test_confirming_twice_never_writes_a_second_row(tmp_path: Path) -> None:
    """Replay safety comes from UNIQUE(user_id, identity_url), not bookkeeping."""

    with _client(tmp_path) as (client, database_path):
        payload = {"urls": ["https://example.com/one", "https://example.com/two"], "confirm": True}
        first = client.post("/api/library/sites/batch", json=payload, headers=ORIGIN)
        second = client.post("/api/library/sites/batch", json=payload, headers=ORIGIN)

        assert first.json()["created"] == 2
        assert second.json()["created"] == 0
        assert second.json()["duplicate"] == 2
        assert _site_count(database_path) == 2


def test_free_text_is_accepted_and_parsed_server_side(tmp_path: Path) -> None:
    with _client(tmp_path) as (client, database_path):
        result = client.post(
            "/api/library/sites/batch",
            json={
                "text": "把这两个存了 https://example.com/x 还有 https://example.com/y 谢谢",
                "confirm": True,
            },
            headers=ORIGIN,
        )
        assert result.status_code == 200, result.text
        assert result.json()["created"] == 2
        assert _site_count(database_path) == 2


def test_a_batch_without_any_url_is_rejected(tmp_path: Path) -> None:
    with _client(tmp_path) as (client, _):
        result = client.post(
            "/api/library/sites/batch",
            json={"text": "帮我存一下那个网站"},
            headers=ORIGIN,
        )
        assert result.status_code == 422
        assert result.json()["detail"]["code"] == "no_urls"


def test_batch_is_account_scoped_and_origin_checked(tmp_path: Path) -> None:
    with _client(tmp_path) as (client, database_path):
        assert (
            client.post(
                "/api/library/sites/batch",
                json={"urls": ["https://example.com/x"], "confirm": True},
            ).status_code
            == 403
        )
        assert _site_count(database_path) == 0

        client.cookies.clear()
        assert (
            client.post(
                "/api/library/sites/batch",
                json={"urls": ["https://example.com/x"]},
                headers=ORIGIN,
            ).status_code
            == 401
        )
