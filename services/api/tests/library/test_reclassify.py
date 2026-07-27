from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from webhub.bookmarks.classification_batches import BoundClassificationMapping
from webhub.bookmarks.classification_contract import ClassificationMapping
from webhub.bookmarks.classifier import BatchRunResult
from webhub.config import Settings
from webhub.db.database import Database
from webhub.db.migrations import upgrade_database
from webhub.db.models import Category, Site, User
from webhub.library.reclassify import (
    ReclassificationError,
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


def test_reclassify_propose_discloses_the_bounded_retry_ceiling(tmp_path: Path, monkeypatch):
    with _client(tmp_path) as (client, _):
        created = client.post(
            "/api/library/sites",
            json={"name": "React", "url": "https://react.dev/learn"},
            headers=ORIGIN,
        )
        assert created.status_code == 201

        async def mock_resolve(*args, **kwargs):
            return MagicMock()

        monkeypatch.setattr(
            "webhub.library.reclassify.resolve_optional_binding",
            mock_resolve,
        )

        response = client.post("/api/library/reclassify/propose", headers=ORIGIN)

        assert response.status_code == 200
        draft = response.json()["draft"]
        assert draft["estimated_request_count"] == 1
        assert draft["maximum_request_count"] == 2
        assert set(draft["expected_categories"].values()) == set(
            draft["allowed_categories"]
        )


def test_reclassify_propose_rejects_a_partial_budget_without_model_calls(
    tmp_path: Path,
    monkeypatch,
):
    with _client(tmp_path) as (client, _):
        created = client.post(
            "/api/library/sites",
            json={"name": "React", "url": "https://react.dev/learn"},
            headers=ORIGIN,
        )
        assert created.status_code == 201

        async def mock_resolve(*args, **kwargs):
            return MagicMock()

        incomplete_plan = MagicMock(
            budget_exhausted_source_ids=("src_react_dev",),
            privacy_excluded_source_ids=(),
            privacy_excluded_member_source_ids=(),
        )
        monkeypatch.setattr(
            "webhub.library.reclassify.resolve_optional_binding",
            mock_resolve,
        )
        monkeypatch.setattr(
            "webhub.library.reclassify.build_candidate_classification_batches",
            lambda **_: incomplete_plan,
        )

        response = client.post("/api/library/reclassify/propose", headers=ORIGIN)

        assert response.status_code == 200
        assert response.json() == {
            "status": "rejected",
            "reason": "资料库规模超出当前全量重分类上限，未发起任何模型请求。",
        }


def test_reclassify_propose_does_not_silently_skip_private_hosts(
    tmp_path: Path,
    monkeypatch,
):
    with _client(tmp_path) as (client, _):
        created = client.post(
            "/api/library/sites",
            json={"name": "Local Admin", "url": "http://localhost:3000/admin"},
            headers=ORIGIN,
        )
        assert created.status_code == 201

        async def mock_resolve(*args, **kwargs):
            return MagicMock()

        monkeypatch.setattr(
            "webhub.library.reclassify.resolve_optional_binding",
            mock_resolve,
        )

        response = client.post("/api/library/reclassify/propose", headers=ORIGIN)

        assert response.status_code == 200
        assert response.json() == {
            "status": "rejected",
            "reason": "资料库包含不能安全发送给模型的本地或私网网址，未发起任何模型请求。",
        }


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


def test_reclassify_apply_moves_site_to_end_of_nonempty_category(tmp_path: Path, monkeypatch):
    """apply 必须真的改 category_id，并为目标分类分配不冲突的位置。

    回归测试：此前 apply 用 getattr(bound, "subject_id") 读 BoundClassificationMapping，
    而那个 dataclass（slots=True）只有 source_id / mapping / used_fallback 三个字段，
    于是每条都取到 None 并 continue —— 花掉用户的 token、返回 status="success"、
    updated_count 恒为 0、一个站点都没动。后来只修改 category_id，又会让原本都在
    position=0 的两个网站撞唯一索引。只断言返回码的测试抓不到这两类问题。
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

        existing = client.post(
            "/api/library/sites",
            json={
                "name": "Existing Frontend Site",
                "url": "https://frontend.example/docs",
                "category_id": target_id,
            },
            headers=ORIGIN,
        )
        assert existing.status_code == 201, existing.text
        existing_id = existing.json()["id"]

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

        async def mock_run_plan(_binding, plan, *, max_concurrency, cancel_requested):
            # 模型把这个 hostname 归到「前端」；source_id 由 prepare_ 侧生成。
            assert all(not batch.include_tags for batch in plan.batches)
            assert max_concurrency == 4
            assert cancel_requested is None
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

        async def scenario() -> tuple[dict, tuple[int, int], tuple[int, int]]:
            database = Database(settings.database_url)
            try:
                async with database.sessions() as session:
                    user_id = await session.scalar(
                        select(User.id).where(User.username == "alice")
                    )
                    assert user_id is not None
                    account_sites = list(
                        (
                            await session.scalars(
                                select(Site).where(Site.user_id == user_id)
                            )
                        ).all()
                    )
                    versions = {site.id: site.version for site in account_sites}
                    categories = dict(
                        (
                            await session.execute(
                                select(Category.id, Category.name).where(
                                    Category.user_id == user_id
                                )
                            )
                        ).all()
                    )
                    result = await apply_reclassification(
                        session,
                        user_id,
                        expected_categories=categories,
                        expected_versions=versions,
                    )
                    moved = await session.scalar(select(Site).where(Site.id == site_id))
                    unchanged = await session.scalar(select(Site).where(Site.id == existing_id))
                    assert moved is not None
                    assert unchanged is not None
                    return (
                        result,
                        (moved.position, moved.version),
                        (unchanged.position, unchanged.version),
                    )
            finally:
                await database.dispose()

        result, moved_state, unchanged_state = asyncio.run(scenario())

        assert result["status"] == "success"
        assert result["updated_count"] == 1, "apply 报告成功却没有更新任何站点"
        assert result["total_sites"] == 2
        assert moved_state == (1, 2)
        assert unchanged_state == (0, 1)

        after = client.get(f"/api/library/sites/{site_id}", headers=ORIGIN)
        assert after.status_code == 200, after.text
        assert after.json()["category"]["id"] == target_id


def test_reclassify_apply_does_not_write_if_client_disconnects_before_commit(
    tmp_path: Path,
    monkeypatch,
):
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
        original = created.json()
        site_id = original["id"]
        original_category_id = original["category"]["id"]

        async def mock_resolve(*args, **kwargs):
            return MagicMock()

        async def mock_run_plan(_binding, plan, *, max_concurrency, cancel_requested):
            assert max_concurrency == 4
            assert cancel_requested is not None
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

        async def scenario() -> tuple[int, tuple[str | None, int, int]]:
            database = Database(settings.database_url)
            disconnect_checks = 0

            async def cancel_requested() -> bool:
                nonlocal disconnect_checks
                disconnect_checks += 1
                # The model has completed. The first post-model check passes,
                # as does the pre-write check. The connection drops only after
                # the conditional UPDATE has run, immediately before commit.
                return disconnect_checks >= 3

            try:
                async with database.sessions() as session:
                    user_id = await session.scalar(
                        select(User.id).where(User.username == "alice")
                    )
                    assert user_id is not None
                    account_sites = list(
                        (
                            await session.scalars(
                                select(Site).where(Site.user_id == user_id)
                            )
                        ).all()
                    )
                    versions = {site.id: site.version for site in account_sites}
                    categories = dict(
                        (
                            await session.execute(
                                select(Category.id, Category.name).where(
                                    Category.user_id == user_id
                                )
                            )
                        ).all()
                    )
                    with pytest.raises(ReclassificationError) as raised:
                        await apply_reclassification(
                            session,
                            user_id,
                            expected_categories=categories,
                            expected_versions=versions,
                            cancel_requested=cancel_requested,
                        )
                    assert raised.value.safe_message == (
                        "连接已中断，重分类结果未写入，请重新发起操作。"
                    )

                async with database.sessions() as verify_session:
                    persisted = await verify_session.scalar(
                        select(Site).where(Site.id == site_id)
                    )
                    assert persisted is not None
                    persisted_state = (
                        persisted.category_id,
                        persisted.position,
                        persisted.version,
                    )
                return disconnect_checks, persisted_state
            finally:
                await database.dispose()

        disconnect_checks, persisted_state = asyncio.run(scenario())

        assert disconnect_checks == 3
        assert persisted_state == (original_category_id, 0, original["version"])


def test_reclassify_apply_rejects_model_time_version_change_atomically(
    tmp_path: Path,
    monkeypatch,
):
    with _client(tmp_path) as (client, database_path):
        database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
        settings = Settings(
            environment="test",
            database_url=database_url,
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

        first = client.post(
            "/api/library/sites",
            json={"name": "React", "url": "https://react.dev/learn"},
            headers=ORIGIN,
        )
        second = client.post(
            "/api/library/sites",
            json={"name": "Vite", "url": "https://vite.dev/guide"},
            headers=ORIGIN,
        )
        assert first.status_code == second.status_code == 201
        first_id = first.json()["id"]
        second_id = second.json()["id"]
        original_category_id = first.json()["category"]["id"]

        async def mock_resolve(*args, **kwargs):
            return MagicMock()

        async def mock_run_plan(_binding, plan, *, max_concurrency, cancel_requested):
            assert max_concurrency == 4
            assert cancel_requested is None
            concurrent_database = Database(database_url)
            try:
                async with concurrent_database.sessions() as concurrent_session:
                    changed = await concurrent_session.execute(
                        update(Site)
                        .where(Site.id == second_id, Site.version == 1)
                        .values(pinned=True, version=Site.version + 1)
                    )
                    assert changed.rowcount == 1  # type: ignore[attr-defined]
                    await concurrent_session.commit()
            finally:
                await concurrent_database.dispose()

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

        async def scenario() -> tuple[
            str,
            tuple[str | None, int, int, bool],
            tuple[str | None, int, int, bool],
        ]:
            database = Database(settings.database_url)
            try:
                async with database.sessions() as session:
                    user_id = await session.scalar(
                        select(User.id).where(User.username == "alice")
                    )
                    assert user_id is not None
                    account_sites = list(
                        (
                            await session.scalars(
                                select(Site).where(Site.user_id == user_id)
                            )
                        ).all()
                    )
                    versions = {site.id: site.version for site in account_sites}
                    categories = dict(
                        (
                            await session.execute(
                                select(Category.id, Category.name).where(
                                    Category.user_id == user_id
                                )
                            )
                        ).all()
                    )
                    with pytest.raises(ReclassificationError) as raised:
                        await apply_reclassification(
                            session,
                            user_id,
                            expected_categories=categories,
                            expected_versions=versions,
                        )
                    safe_message = raised.value.safe_message

                async with database.sessions() as verify_session:
                    stored = {
                        site.id: site
                        for site in (
                            await verify_session.scalars(
                                select(Site).where(Site.id.in_([first_id, second_id]))
                            )
                        ).all()
                    }
                    first_state = (
                        stored[first_id].category_id,
                        stored[first_id].position,
                        stored[first_id].version,
                        stored[first_id].pinned,
                    )
                    second_state = (
                        stored[second_id].category_id,
                        stored[second_id].position,
                        stored[second_id].version,
                        stored[second_id].pinned,
                    )
                return safe_message, first_state, second_state
            finally:
                await database.dispose()

        safe_message, first_state, second_state = asyncio.run(scenario())

        assert safe_message == "资料库状态已发生变化，请重新发起重分类草稿。"
        assert first_state == (original_category_id, 0, 1, False)
        assert second_state == (original_category_id, 1, 2, True)


def test_reclassify_apply_rejects_model_time_taxonomy_change(
    tmp_path: Path,
    monkeypatch,
):
    with _client(tmp_path) as (client, database_path):
        database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
        settings = Settings(
            environment="test",
            database_url=database_url,
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
        original_category_id = created.json()["category"]["id"]

        async def mock_resolve(*args, **kwargs):
            return MagicMock()

        async def mock_run_plan(_binding, plan, *, max_concurrency, cancel_requested):
            assert max_concurrency == 4
            assert cancel_requested is None
            concurrent_database = Database(database_url)
            try:
                async with concurrent_database.sessions() as concurrent_session:
                    renamed = await concurrent_session.execute(
                        update(Category)
                        .where(Category.id == target_id)
                        .values(name="前端工具", normalized_name="前端工具")
                    )
                    assert renamed.rowcount == 1  # type: ignore[attr-defined]
                    await concurrent_session.commit()
            finally:
                await concurrent_database.dispose()

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

        async def scenario() -> tuple[str, tuple[str | None, int], str]:
            database = Database(settings.database_url)
            try:
                async with database.sessions() as session:
                    user_id = await session.scalar(
                        select(User.id).where(User.username == "alice")
                    )
                    assert user_id is not None
                    site = await session.scalar(select(Site).where(Site.id == site_id))
                    assert site is not None
                    categories = dict(
                        (
                            await session.execute(
                                select(Category.id, Category.name).where(
                                    Category.user_id == user_id
                                )
                            )
                        ).all()
                    )
                    with pytest.raises(ReclassificationError) as raised:
                        await apply_reclassification(
                            session,
                            user_id,
                            expected_categories=categories,
                            expected_versions={site.id: site.version},
                        )
                    safe_message = raised.value.safe_message

                async with database.sessions() as verify_session:
                    persisted_site = await verify_session.scalar(
                        select(Site).where(Site.id == site_id)
                    )
                    persisted_category_name = await verify_session.scalar(
                        select(Category.name).where(Category.id == target_id)
                    )
                    assert persisted_site is not None
                    assert persisted_category_name is not None
                    site_state = (persisted_site.category_id, persisted_site.version)
                return safe_message, site_state, persisted_category_name
            finally:
                await database.dispose()

        safe_message, site_state, persisted_category_name = asyncio.run(scenario())

        assert safe_message == "分类结构已发生变化，请重新发起重分类草稿。"
        assert site_state == (original_category_id, 1)
        assert persisted_category_name == "前端工具"


def test_reclassify_apply_rejects_partial_version_snapshot_before_model_call(
    tmp_path: Path,
    monkeypatch,
):
    with _client(tmp_path) as (client, _):
        first = client.post(
            "/api/library/sites",
            json={"name": "React", "url": "https://react.dev/learn"},
            headers=ORIGIN,
        )
        second = client.post(
            "/api/library/sites",
            json={"name": "Vite", "url": "https://vite.dev/guide"},
            headers=ORIGIN,
        )
        assert first.status_code == second.status_code == 201

        async def mock_resolve(*args, **kwargs):
            return MagicMock()

        async def fail_if_called(*args, **kwargs):
            raise AssertionError("partial version snapshots must fail before model calls")

        monkeypatch.setattr(
            "webhub.library.reclassify.resolve_optional_binding",
            mock_resolve,
        )
        monkeypatch.setattr("webhub.library.reclassify.run_plan", fail_if_called)

        categories_response = client.get("/api/library/categories", headers=ORIGIN)
        assert categories_response.status_code == 200
        expected_categories = {
            category["id"]: category["name"]
            for category in categories_response.json()["items"]
        }

        response = client.post(
            "/api/library/reclassify/apply",
            json={
                "expected_categories": expected_categories,
                "expected_versions": {first.json()["id"]: first.json()["version"]},
            },
            headers=ORIGIN,
        )

        assert response.status_code == 409
        assert response.json()["detail"] == "资料库状态已发生变化，请重新发起重分类草稿。"


def test_reclassify_apply_rejects_taxonomy_changed_after_proposal_without_model_call(
    tmp_path: Path,
    monkeypatch,
):
    with _client(tmp_path) as (client, _):
        created = client.post(
            "/api/library/sites",
            json={"name": "React", "url": "https://react.dev/learn"},
            headers=ORIGIN,
        )
        assert created.status_code == 201

        async def mock_resolve(*args, **kwargs):
            return MagicMock()

        async def fail_if_called(*args, **kwargs):
            raise AssertionError("stale taxonomy must fail before model calls")

        monkeypatch.setattr(
            "webhub.library.reclassify.resolve_optional_binding",
            mock_resolve,
        )
        proposal = client.post("/api/library/reclassify/propose", headers=ORIGIN)
        assert proposal.status_code == 200
        draft = proposal.json()["draft"]

        added = client.post(
            "/api/library/categories",
            json={"name": "提案后新增分类"},
            headers=ORIGIN,
        )
        assert added.status_code == 201
        monkeypatch.setattr("webhub.library.reclassify.run_plan", fail_if_called)

        response = client.post(
            "/api/library/reclassify/apply",
            json={
                "expected_categories": draft["expected_categories"],
                "expected_versions": draft["expected_versions"],
            },
            headers=ORIGIN,
        )

        assert response.status_code == 409
        assert response.json()["detail"] == "分类结构已发生变化，请重新发起重分类草稿。"
