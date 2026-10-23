from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from webhub.config import Settings
from webhub.db.migrations import upgrade_database
from webhub.ingestion.worker import AnalysisSchedule
from webhub.main import create_app

ORIGIN = {"Origin": "http://testserver"}


@dataclass
class Environment:
    client: TestClient
    directory: Path
    database_url: str

    def register(self, name: str) -> str:
        response = self.client.post(
            "/api/auth/register",
            json={"username": name, "password": "a sufficiently secure password"},
            headers=ORIGIN,
        )
        assert response.status_code == 201, response.text
        return self.client.cookies.get("webhub_session") or ""

    def query(self, sql: str, *params: object) -> list[Any]:
        with sqlite3.connect(self.directory / "main.sqlite3") as connection:
            return connection.execute(sql, params).fetchall()

    def execute(self, sql: str, *params: object) -> None:
        with sqlite3.connect(self.directory / "main.sqlite3") as connection:
            connection.execute(sql, params)
            connection.commit()

    def post(self, path: str, payload: object) -> Any:
        response = self.client.post(path, json=payload, headers=ORIGIN)
        assert response.status_code in {200, 201}, response.text
        return response.json()


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Environment]:
    url = f"sqlite+aiosqlite:///{tmp_path / 'main.sqlite3'}"
    settings = Settings(environment="test", database_url=url, data_directory=tmp_path)
    upgrade_database(url)
    monkeypatch.setattr(
        "webhub.ingestion.worker.schedule_analysis",
        lambda *_args, **_kwargs: AnalysisSchedule(queued=0, already_queued=0, rejected=0),
    )
    monkeypatch.setattr(
        "webhub.ingestion.worker.ensure_auto_backfill", lambda *_args, **_kwargs: False
    )
    with TestClient(create_app(settings=settings)) as client:
        environment = Environment(client, tmp_path, url)
        environment.register("alpha")
        yield environment


@pytest.fixture
def backup() -> dict[str, Any]:
    return {
        "format": "webhub-backup",
        "version": 1,
        "exported_at": "2026-10-23T08:00:00Z",
        "account": {"username": "source", "display_name": "源账号"},
        "categories": [{"key": "c1", "name": "AI 与 Agent", "icon": "Bot", "is_default": True}],
        "tags": [{"key": "t1", "name": "Agent"}],
        "sites": [
            {
                "key": "s1",
                "url": "https://example.org/tool",
                "name": "测试收藏",
                "summary": "",
                "description": "收藏详情",
                "kind": "website",
                "kind_source": "manual",
                "category_key": "c1",
                "tag_keys": ["t1"],
                "pinned": False,
                "favicon_url": None,
                "preview_url": None,
                "meta": {"github": {"stars": 20}},
                "created_at": "2026-10-01T08:00:00Z",
                "open_count": 8,
                "last_opened_at": "2026-10-23T08:00:00Z",
                "seen_at": "2026-10-10T08:00:00Z",
                "starred_at": "2026-10-12T08:00:00Z",
                "resurface_muted": True,
            }
        ],
        "spaces": [{"key": "p1", "name": "设计工具", "site_keys": ["s1"]}],
        "feeds": [
            {
                "url": "https://example.org/feed.xml",
                "title": "示例订阅",
                "kind": "rss",
                "enabled": False,
            }
        ],
        "conversations": [
            {
                "title": "保存的对话",
                "created_at": "2026-10-22T08:00:00Z",
                "messages": [
                    {"role": "user", "text": "可见正文", "created_at": "2026-10-22T08:00:00Z"}
                ],
            }
        ],
    }
