from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from webhub.config import Settings
from webhub.db.database import Database
from webhub.db.migrations import upgrade_database
from webhub.db.models import User
from webhub.main import create_app
from webhub.search.backfill import backfill_embeddings, index_status

ORIGIN = {"Origin": "http://testserver"}
_REAL_ASYNC_CLIENT = httpx.AsyncClient


@dataclass(frozen=True, slots=True)
class Endpoint:
    base_url: str = "https://api.example.com/v1"
    model_name: str | None = "embed-1"
    timeout_seconds: int = 5
    client_api_key: str = "sk-account-secret"


@contextmanager
def _account(tmp_path: Path, site_count: int) -> Iterator[Settings]:
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'main.sqlite3').as_posix()}",
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
        for index in range(site_count):
            created = client.post(
                "/api/library/sites",
                json={
                    "name": f"站点 {index}",
                    "url": f"https://site-{index}.example.com",
                    "description": f"第 {index} 个站点的说明",
                },
                headers=ORIGIN,
            )
            assert created.status_code == 201, created.text
    yield settings


def _mock(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(record)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return seen


def _vectors(request: httpx.Request) -> httpx.Response:
    import json

    count = len(json.loads(request.content)["input"])
    return httpx.Response(
        200,
        json={"data": [{"index": i, "embedding": [1.0, 0.0]} for i in range(count)]},
    )


def _run(settings: Settings, scenario) -> object:
    async def wrapped() -> object:
        database = Database(settings.database_url)
        try:
            async with database.sessions() as session:
                return await scenario(session)
        finally:
            await database.dispose()

    return asyncio.run(wrapped())


async def _user_id(session) -> str:
    return str(await session.scalar(select(User.id).where(User.username == "alice")))


def test_a_second_pass_over_an_unchanged_library_spends_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The criterion stated as money: re-running must not re-buy the vectors."""

    with _account(tmp_path, 5) as settings:
        seen = _mock(monkeypatch, _vectors)

        async def scenario(session) -> tuple[int, int, int]:
            user_id = await _user_id(session)
            first = await backfill_embeddings(session, user_id, binding=Endpoint())
            after_first = len(seen)
            second = await backfill_embeddings(session, user_id, binding=Endpoint())
            return first.embedded, after_first, second.requests

        embedded, after_first, second_requests = _run(settings, scenario)  # type: ignore[misc]

    assert embedded == 5
    assert after_first == 1, "5 个站点应当合成一次批量请求"
    assert second_requests == 0, "第二次回填必须一个厂商请求都不发"


def test_a_failing_vendor_does_not_retry_in_a_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A vendor outage must not burn the quota — one attempt per batch, then stop."""

    with _account(tmp_path, 5) as settings:
        seen = _mock(monkeypatch, lambda _r: httpx.Response(500, json={"error": "boom"}))

        async def scenario(session) -> tuple[int, int, int]:
            user_id = await _user_id(session)
            result = await backfill_embeddings(session, user_id, binding=Endpoint())
            return result.embedded, result.failed_batches, len(seen)

        embedded, failed, calls = _run(settings, scenario)  # type: ignore[misc]

    assert embedded == 0
    assert failed == 1
    assert calls == 1, "整批失败后不得重试"


def test_the_estimate_matches_what_the_backfill_actually_sends(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cost estimate the user is shown must be the number actually spent."""

    with _account(tmp_path, 5) as settings:
        _mock(monkeypatch, _vectors)

        async def scenario(session) -> tuple[int, int, int, int]:
            user_id = await _user_id(session)
            before = await index_status(session, user_id, binding=Endpoint())
            result = await backfill_embeddings(session, user_id, binding=Endpoint())
            after = await index_status(session, user_id, binding=Endpoint())
            return before.estimated_requests, result.requests, after.pending, after.indexed

        estimated, actual, pending_after, indexed_after = _run(settings, scenario)  # type: ignore[misc]

    assert estimated == actual
    assert pending_after == 0
    assert indexed_after == 5


def test_an_unconfigured_account_is_never_told_it_has_work_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a Provider there is nothing to spend, so pending must read 0."""

    with _account(tmp_path, 5) as settings:
        seen = _mock(monkeypatch, _vectors)

        async def scenario(session) -> tuple[bool, int, int, int]:
            user_id = await _user_id(session)
            status = await index_status(session, user_id, binding=None)
            return status.configured, status.pending, status.estimated_requests, status.total_sites

        configured, pending, estimated, total = _run(settings, scenario)  # type: ignore[misc]

    assert configured is False
    assert pending == 0
    assert estimated == 0
    assert total == 5, "站点总数仍要如实报告"
    assert seen == [], "未配 Provider 时不得有任何厂商调用"
