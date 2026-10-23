from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.config import Settings
from webhub.db.database import Database
from webhub.db.migrations import upgrade_database
from webhub.db.models import (
    Category,
    ProviderConfig,
    Site,
    SiteAnalysisIntent,
    SiteMetadataPreference,
    User,
)
from webhub.ingestion import intents, worker
from webhub.library import service as library_service
from webhub.library.analysis_queue import schedule_site_enrichment
from webhub.library.schemas import SiteUpdateRequest
from webhub.main import create_app

NOW = datetime(2026, 10, 23, tzinfo=UTC)


def _database(tmp_path: Path) -> Database:
    url = f"sqlite+aiosqlite:///{tmp_path / 'main.sqlite3'}"
    upgrade_database(url)
    return Database(url)


async def _seed(database: Database, user_id: str = "alice", *, provider: bool = True) -> str:
    site_id = f"{user_id}-site"
    async with database.sessions() as session:
        session.add(User(id=user_id, username=user_id, display_name=user_id, password_hash="test"))
        await session.flush()
        session.add(
            Category(
                id=f"{user_id}-category",
                user_id=user_id,
                name="未分类",
                normalized_name="未分类",
                is_default=True,
            )
        )
        await session.flush()
        session.add(
            Site(
                id=site_id,
                user_id=user_id,
                category_id=f"{user_id}-category",
                name="收藏",
                normalized_name="收藏",
                original_url="https://example.test/",
                identity_url="https://example.test/",
                position=0,
            )
        )
        if provider:
            session.add(
                ProviderConfig(
                    user_id=user_id,
                    kind="model",
                    provider="openai_compatible",
                    display_name="Model",
                    enabled=True,
                )
            )
        await session.commit()
    return site_id


async def _row(database: Database, user_id: str = "alice") -> SiteAnalysisIntent | None:
    async with database.sessions() as session:
        return await session.get(SiteAnalysisIntent, (user_id, f"{user_id}-site"))


def _fake_schedule(
    monkeypatch: pytest.MonkeyPatch, *, rejected: bool = False
) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    def schedule(_database: Database, **kwargs: object) -> worker.AnalysisSchedule:
        calls.append(kwargs)
        return worker.AnalysisSchedule(
            queued=int(not rejected), already_queued=0, rejected=int(rejected)
        )

    monkeypatch.setattr(worker, "schedule_analysis", schedule)
    return calls


def test_record_keeps_one_row_and_preserves_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    calls = _fake_schedule(monkeypatch)

    async def scenario() -> None:
        site_id = await _seed(database)
        await schedule_site_enrichment(database, user_id="alice", site_id=site_id)
        async with database.sessions() as session:
            row = await session.get(SiteAnalysisIntent, ("alice", site_id))
            assert row is not None
            row.attempts = 3
            await session.commit()
        await schedule_site_enrichment(database, user_id="alice", site_id=site_id)
        async with database.sessions() as session:
            rows = (await session.scalars(select(SiteAnalysisIntent))).all()
            assert len(rows) == 1
            assert rows[0].attempts == 3
        assert len(calls) == 2
        assert calls[0]["interactive"] is True
        assert calls[0]["priority"] is True
        await database.dispose()

    asyncio.run(scenario())


def test_restart_dispatches_and_duplicate_tick_is_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)

    async def scenario() -> None:
        site_id = await _seed(database)
        await intents.record_site_intent(database, user_id="alice", site_id=site_id)
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def analyze(
            _database: Database,
            user_id: str,
            target_id: str,
            **kwargs: object,
        ) -> None:
            calls.append((user_id, target_id, kwargs["use_llm"]))
            entered.set()
            await release.wait()

        monkeypatch.setattr(worker, "analyze_in_background", analyze)
        worker.start(database)
        try:
            now = datetime.now(UTC) + timedelta(seconds=1)
            assert (await intents.tick(database, now=now)).scheduled == 1
            await asyncio.wait_for(entered.wait(), 5)
            assert (await intents.tick(database, now=now + timedelta(seconds=60))).scheduled == 1
            assert calls == [("alice", site_id, True)]
        finally:
            release.set()
            await worker.shutdown(database)
            await database.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("reason", ["missing", "completed", "no_provider", "exhausted"])
def test_tick_removes_terminal_intents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    database = _database(tmp_path)
    calls = _fake_schedule(monkeypatch)
    warnings: list[tuple[object, ...]] = []
    monkeypatch.setattr(intents._LOGGER, "warning", lambda *args: warnings.append(args))

    async def scenario() -> None:
        site_id = await _seed(database, provider=reason != "no_provider")
        await intents.record_site_intent(database, user_id="alice", site_id=site_id)
        if reason == "missing":
            # Model an orphan left by an older writer with foreign keys disabled.
            with sqlite3.connect(tmp_path / "main.sqlite3") as connection:
                connection.execute("DELETE FROM sites WHERE id = ?", (site_id,))
        async with database.sessions() as session:
            if reason == "completed":
                session.add(
                    SiteMetadataPreference(user_id="alice", site_id=site_id, llm_analyzed_at=NOW)
                )
            if reason == "exhausted":
                row = await session.get(SiteAnalysisIntent, ("alice", site_id))
                row.attempts = intents.MAX_INTENT_ATTEMPTS
            await session.commit()
        result = await intents.tick(database, now=datetime.now(UTC) + timedelta(seconds=1))
        assert result == intents.IntentTickResult(
            completed=int(reason == "completed"), dropped=int(reason != "completed")
        )
        assert await _row(database) is None
        assert calls == []
        if reason == "exhausted":
            assert warnings == [("site analysis intent exhausted site_id=%s", site_id)]
        await database.dispose()

    asyncio.run(scenario())


def test_tick_backoff_and_full_queue_deferral(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    _fake_schedule(monkeypatch)

    async def scenario() -> None:
        site_id = await _seed(database)
        await intents.record_site_intent(database, user_id="alice", site_id=site_id)
        now = datetime.now(UTC) + timedelta(seconds=1)
        for attempts in range(1, 7):
            assert (await intents.tick(database, now=now)).scheduled == 1
            row = await _row(database)
            assert row.attempts == attempts
            expected = now + timedelta(seconds=60 * 2 ** (attempts - 1))
            assert row.next_attempt_at.replace(tzinfo=UTC) == expected
            now = expected
        # The cap is defensive beyond the six-attempt retry budget.
        assert intents._backoff(20) == timedelta(hours=6)
        async with database.sessions() as session:
            row = await session.get(SiteAnalysisIntent, ("alice", site_id))
            row.attempts = 0
            await session.commit()
        _fake_schedule(monkeypatch, rejected=True)
        assert (await intents.tick(database, now=now)).deferred == 1
        row = await _row(database)
        assert row.attempts == 0
        assert row.next_attempt_at.replace(tzinfo=UTC) == now + timedelta(seconds=60)
        await database.dispose()

    asyncio.run(scenario())


def test_tick_account_scope_order_limit_and_cascades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    calls = _fake_schedule(monkeypatch)

    async def scenario() -> None:
        for user_id in ("alice", "bob"):
            site_id = await _seed(database, user_id)
            await intents.record_site_intent(database, user_id=user_id, site_id=site_id)
        now = datetime.now(UTC) + timedelta(seconds=1)
        assert (await intents.tick(database, now=now, limit=1)).scheduled == 1
        assert calls[0]["user_id"] == "alice"
        assert calls[0]["site_ids"] == ("alice-site",)
        assert (await intents.tick(database, now=now, limit=1)).scheduled == 1
        assert calls[1]["user_id"] == "bob"
        assert calls[1]["site_ids"] == ("bob-site",)
        async with database.sessions() as session:
            await session.execute(delete(Site).where(Site.user_id == "alice"))
            await session.execute(delete(User).where(User.id == "bob"))
            await session.commit()
        assert await _row(database) is None
        assert await _row(database, "bob") is None
        await database.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("persistence_fails", [False, True])
def test_create_success_survives_intent_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, persistence_fails: bool
) -> None:
    database = _database(tmp_path)
    _fake_schedule(monkeypatch)
    if persistence_fails:

        async def fail(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("secret upstream text https://private.example/?key=hidden")

        monkeypatch.setattr(intents, "record_site_intent", fail)
    settings = Settings(
        environment="test", database_url=database.database_url, data_directory=tmp_path
    )
    with TestClient(create_app(settings=settings, database=database)) as client:
        headers = {"Origin": "http://testserver"}
        assert (
            client.post(
                "/api/auth/register",
                headers=headers,
                json={
                    "username": "alice",
                    "password": "a sufficiently secure password",
                },
            ).status_code
            == 201
        )
        response = client.post(
            "/api/library/sites",
            headers=headers,
            json={"name": "保存", "url": "https://example.test/"},
        )
        assert response.status_code == 201
        with sqlite3.connect(tmp_path / "main.sqlite3") as connection:
            count = connection.execute("SELECT count(*) FROM site_analysis_intents").fetchone()[0]
            assert count == int(not persistence_fails)
    asyncio.run(database.dispose())


def test_tick_does_not_drop_a_new_intent_after_url_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    _fake_schedule(monkeypatch)

    async def scenario() -> None:
        site_id = await _seed(database)
        await intents.record_site_intent(database, user_id="alice", site_id=site_id)
        async with database.sessions() as session:
            session.add(
                SiteMetadataPreference(
                    user_id="alice",
                    site_id=site_id,
                    llm_analyzed_at=NOW,
                )
            )
            await session.commit()
        completion_read, release_tick, edit_started = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        original_get = AsyncSession.get

        async def paused_get(
            session: AsyncSession,
            entity: object,
            ident: object,
            **kwargs: object,
        ) -> object:
            row = await original_get(session, entity, ident, **kwargs)
            if entity is SiteMetadataPreference and not completion_read.is_set():
                completion_read.set()
                await release_tick.wait()
            return row

        monkeypatch.setattr(AsyncSession, "get", paused_get)

        async def retarget() -> None:
            edit_started.set()
            async with database.sessions() as session:
                await library_service.update_site(
                    session,
                    "alice",
                    site_id,
                    SiteUpdateRequest(expected_version=1, url="https://new.example.test/"),
                )
            await schedule_site_enrichment(database, user_id="alice", site_id=site_id)

        ticking = asyncio.create_task(
            intents.tick(
                database,
                now=datetime.now(UTC) + timedelta(seconds=1),
            )
        )
        try:
            await asyncio.wait_for(completion_read.wait(), 5)
            with (
                sqlite3.connect(tmp_path / "main.sqlite3", timeout=0) as probe,
                pytest.raises(sqlite3.OperationalError, match="database is locked"),
            ):
                probe.execute("UPDATE sites SET version=version WHERE id=?", (site_id,))
            editing = asyncio.create_task(retarget())
            await asyncio.wait_for(edit_started.wait(), 5)
            assert not editing.done()
            release_tick.set()
            result, _ = await asyncio.wait_for(asyncio.gather(ticking, editing), 5)
            assert result.completed == 1
            assert await _row(database) is not None
            async with database.sessions() as session:
                site = await session.get(Site, site_id)
                preference = await session.get(SiteMetadataPreference, ("alice", site_id))
                assert site.original_url == "https://new.example.test/"
                assert site.version == 2
                assert preference is None or preference.llm_analyzed_at is None
        finally:
            release_tick.set()
            await database.dispose()

    asyncio.run(scenario())
