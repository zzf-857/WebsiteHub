from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from webhub.config import Settings
from webhub.db.migrations import upgrade_database
from webhub.db.models import Category, Site
from webhub.library.reclassify import (
    prepare_reclassification_sources,
)
from webhub.main import create_app

ORIGIN = {"Origin": "http://testserver"}


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


def _make_site(site_id: str, name: str, url: str, category_name: str = "技术"):
    site = MagicMock(spec=Site)
    site.id = site_id
    site.name = name
    site.original_url = url
    site.version = 1
    site.category_id = f"cat_{category_name}"
    site.category = MagicMock(spec=Category)
    site.category.name = category_name
    site.tags = []
    return site


def test_prepare_reclassification_sources_aggregates_by_hostname():
    s1 = _make_site("s1", "React Docs", "https://react.dev/learn", "开发")
    s2 = _make_site("s2", "React Reference", "https://react.dev/reference", "开发")
    s3 = _make_site("s3", "Vite", "https://vitejs.dev", "工具")

    cat_by_id = {"cat_开发": "开发", "cat_工具": "工具"}
    sources, mapping = prepare_reclassification_sources([s1, s2, s3], cat_by_id)

    assert len(sources) == 2
    react_source = next(src for src in sources if src.hostname == "react.dev")
    assert react_source.occurrence_count == 2
    assert len(mapping[react_source.source_id]) == 2



def test_reclassify_propose_rejects_without_provider(tmp_path: Path):
    with _client(tmp_path) as (client, _):
        res = client.post("/api/library/reclassify/propose", headers=ORIGIN)
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "rejected"
        assert "未配置或启用模型 Provider" in data["reason"]


def test_reclassify_propose_noop_when_empty(tmp_path: Path, monkeypatch):
    with _client(tmp_path) as (client, _):
        # Mock provider active binding
        async def mock_resolve(*args, **kwargs):
            return MagicMock()

        monkeypatch.setattr(
            "webhub.library.reclassify.resolve_optional_binding",
            mock_resolve,
        )

        res = client.post("/api/library/reclassify/propose", headers=ORIGIN)
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "noop"
        assert "没有需要分类的网站" in data["message"]
