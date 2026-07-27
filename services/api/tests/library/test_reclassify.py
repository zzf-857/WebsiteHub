from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from sqlalchemy import select

from webhub.bookmarks.classification_batches import BoundClassificationMapping
from webhub.bookmarks.classification_contract import ClassificationMapping
from webhub.bookmarks.classifier import BatchRunResult
from webhub.config import Settings
from webhub.db.database import Database
from webhub.db.migrations import upgrade_database
from webhub.db.models import Category, Site, User
from webhub.library.reclassify import (
    apply_reclassification,
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


def _bound(source_id: str, category_id: str, category_name: str) -> BoundClassificationMapping:
    """一条模型答复，绑回本地 source_id —— run_batch 真实返回的就是这个形状。"""

    return BoundClassificationMapping(
        source_id=source_id,
        mapping=ClassificationMapping(
            subject_id=source_id,
            category_action="existing",
            category_id=category_id,
            category_name=category_name,
            tags=("文档", "前端"),
            confidence=0.9,
            needs_review=False,
            reason_code="host_match",
        ),
        used_fallback=False,
    )


def test_reclassify_apply_actually_moves_sites(tmp_path: Path, monkeypatch):
    """apply 必须真的改 category_id。

    回归测试：此前 apply 用 getattr(bound, "subject_id") 读 BoundClassificationMapping，
    而那个 dataclass（slots=True）只有 source_id / mapping / used_fallback 三个字段，
    于是每条都取到 None 并 continue —— 花掉用户的 token、返回 status="success"、
    updated_count 恒为 0、一个站点都没动。只断言返回码的测试抓不到这个。
    """

    with _client(tmp_path) as (client, database_path):
        settings = Settings(
            environment="test",
            database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
            data_directory=tmp_path,
            provider_master_key=b"provider-test-master-key-32bytes",
        )
        target = client.post(
            "/api/library/categories",
            json={"name": "前端"},
            headers=ORIGIN,
        )
        assert target.status_code == 201, target.text
        target_id = target.json()["id"]

        created = client.post(
            "/api/library/sites",
            json={"name": "React", "url": "https://react.dev/learn"},
            headers=ORIGIN,
        )
        assert created.status_code == 201, created.text
        site_id = created.json()["id"]
        assert created.json()["category"]["id"] != target_id

        async def mock_resolve(*args, **kwargs):
            return MagicMock()

        async def mock_run_plan(_binding, plan):
            # 模型把这个 hostname 归到「前端」；source_id 由 prepare_ 侧生成。
            return [
                BatchRunResult(
                    batch_id=batch.batch_id,
                    mappings=tuple(
                        _bound(binding.source_id, target_id, "前端")
                        for binding in batch.bindings
                    ),
                    unresolved_source_ids=(),
                )
                for batch in plan.batches
            ]

        monkeypatch.setattr(
            "webhub.library.reclassify.resolve_optional_binding",
            mock_resolve,
        )
        monkeypatch.setattr("webhub.library.reclassify.run_plan", mock_run_plan)

        async def scenario() -> dict:
            database = Database(settings.database_url)
            try:
                async with database.sessions() as session:
                    user_id = await session.scalar(
                        select(User.id).where(User.username == "alice")
                    )
                    assert user_id is not None
                    site = await session.scalar(select(Site).where(Site.id == site_id))
                    assert site is not None
                    return await apply_reclassification(
                        session,
                        user_id,
                        expected_versions={site_id: site.version},
                    )
            finally:
                await database.dispose()

        result = asyncio.run(scenario())

        assert result["status"] == "success"
        assert result["updated_count"] == 1, "apply 报告成功却没有更新任何站点"

        after = client.get(f"/api/library/sites/{site_id}", headers=ORIGIN)
        assert after.status_code == 200, after.text
        assert after.json()["category"]["id"] == target_id
