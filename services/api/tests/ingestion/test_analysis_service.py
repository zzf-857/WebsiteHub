from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urldefrag, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from webhub.config import Settings
from webhub.db.database import Database
from webhub.db.migrations import upgrade_database
from webhub.ingestion import service as ingestion_service
from webhub.ingestion import worker as ingestion_worker
from webhub.ingestion.fetcher import FetchOutcome, SiteMetadata
from webhub.ingestion.worker import AnalysisSchedule
from webhub.library import service as library_service
from webhub.library.schemas import SiteUpdateRequest
from webhub.main import create_app
from webhub.providers.targets import ResolvedConnectionTarget

ORIGIN = {"Origin": "http://testserver"}
_REAL_ASYNC_CLIENT = httpx.AsyncClient

PAGE = """<!doctype html><html><head>
<title>抓来的标题</title>
<meta name="description" content="抓来的描述">
<meta property="og:image" content="/preview.png">
<link rel="icon" href="/favicon.ico">
</head><body></body></html>"""


@contextmanager
def _client(tmp_path: Path) -> Iterator[TestClient]:
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
        yield client


def _stub_page(monkeypatch: pytest.MonkeyPatch, body: str = PAGE, status: int = 200) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        if _request.url.path == "/favicon.ico":
            return httpx.Response(
                200,
                content=b"\x00\x00\x01\x00",
                headers={"content-type": "image/x-icon"},
            )
        return httpx.Response(
            status, content=body.encode(), headers={"content-type": "text/html; charset=utf-8"}
        )

    def factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", factory)

    async def allow(value: str, **_kwargs: object) -> ResolvedConnectionTarget:
        logical_url = urldefrag(value.strip()).url
        parsed = urlsplit(logical_url)
        assert parsed.hostname is not None
        return ResolvedConnectionTarget(
            url=logical_url,
            hostname=parsed.hostname,
            port=parsed.port or (443 if parsed.scheme == "https" else 80),
            addresses=(ip_address("93.184.216.34"),),
        )

    monkeypatch.setattr("webhub.ingestion.fetcher.resolve_resource_target", allow)


def _site(client: TestClient, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {"name": "手填的名字", "url": "https://example.com/"}
    payload.update(overrides)
    created = client.post("/api/library/sites", json=payload, headers=ORIGIN)
    assert created.status_code == 201, created.text
    return created.json()


def test_analysis_fills_an_empty_description_and_icon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_page(monkeypatch)
    with _client(tmp_path) as client:
        site = _site(client)
        assert not site["description"]

        analyzed = client.post(f"/api/library/sites/{site['id']}/analyze", headers=ORIGIN)
        assert analyzed.status_code == 200, analyzed.text
        body = analyzed.json()
        assert body["analysis_status"] == "complete"
        assert body["description"] == "抓来的描述"
        assert body["favicon_url"] == "https://example.com/favicon.ico"
        assert body["preview_url"] == "https://example.com/preview.png"


def test_analysis_never_overwrites_what_the_user_typed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typed sentence is a decision; a meta tag is a guess about it. Guesses lose."""

    _stub_page(monkeypatch)
    with _client(tmp_path) as client:
        site = _site(client, description="我自己写的说明")

        analyzed = client.post(f"/api/library/sites/{site['id']}/analyze", headers=ORIGIN)
        assert analyzed.status_code == 200
        body = analyzed.json()
        assert body["analysis_status"] == "complete"
        # Description untouched, and the name was never a candidate at all.
        assert body["description"] == "我自己写的说明"
        assert body["name"] == "手填的名字"


def test_analysis_status_reaches_a_terminal_state_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never leave a row in `pending`; that reads as "still working" forever."""

    _stub_page(monkeypatch, status=500)
    with _client(tmp_path) as client:
        site = _site(client)
        analyzed = client.post(f"/api/library/sites/{site['id']}/analyze", headers=ORIGIN)
        assert analyzed.status_code == 200
        assert analyzed.json()["analysis_status"] == "failed"


def test_a_page_without_metadata_is_limited_not_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_page(monkeypatch, body="<html><head></head><body><div id=root></div></body></html>")
    with _client(tmp_path) as client:
        site = _site(client)
        analyzed = client.post(f"/api/library/sites/{site['id']}/analyze", headers=ORIGIN)
        assert analyzed.json()["analysis_status"] == "limited"


def test_analysis_is_account_scoped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_page(monkeypatch)
    with _client(tmp_path) as client:
        site = _site(client)
        client.cookies.clear()
        assert (
            client.post(
                "/api/auth/register",
                json={"username": "bob", "password": "another secure password here"},
                headers=ORIGIN,
            ).status_code
            == 201
        )
        stolen = client.post(f"/api/library/sites/{site['id']}/analyze", headers=ORIGIN)
        assert stolen.status_code == 404


def test_analysis_requires_a_trusted_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_page(monkeypatch)
    with _client(tmp_path) as client:
        site = _site(client)
        assert client.post(f"/api/library/sites/{site['id']}/analyze").status_code == 403


def test_in_flight_analysis_preserves_fields_the_user_fills(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "webhub.library.routes.ingestion_worker.schedule_analysis",
        lambda *_args, **_kwargs: AnalysisSchedule(queued=0, already_queued=0, rejected=0),
    )

    with _client(tmp_path) as client:
        site = _site(client)
        with sqlite3.connect(tmp_path / "main.sqlite3") as connection:
            user_id = str(
                connection.execute(
                    "SELECT user_id FROM sites WHERE id = ?", (site["id"],)
                ).fetchone()[0]
            )

        async def scenario() -> None:
            fetch_started = asyncio.Event()
            release_fetch = asyncio.Event()

            async def delayed_fetch(url: str, *, timeout_seconds: int) -> FetchOutcome:
                assert url == "https://example.com/"
                assert timeout_seconds > 0
                fetch_started.set()
                await asyncio.wait_for(release_fetch.wait(), timeout=5)
                return FetchOutcome(
                    status="complete",
                    reason="ok",
                    metadata=SiteMetadata(
                        description="抓来的描述",
                        icon_url="https://example.com/favicon.ico",
                        image_url="https://example.com/preview.png",
                    ),
                )

            monkeypatch.setattr(ingestion_service, "fetch_site_metadata", delayed_fetch)
            database = Database(
                f"sqlite+aiosqlite:///{(tmp_path / 'main.sqlite3').as_posix()}"
            )
            try:
                async with database.sessions() as analysis_session:
                    task = asyncio.create_task(
                        ingestion_service.analyze_site(
                            analysis_session,
                            user_id,
                            str(site["id"]),
                        )
                    )
                    await asyncio.wait_for(fetch_started.wait(), timeout=5)
                    async with database.sessions() as edit_session:
                        edited = await library_service.update_site(
                            edit_session,
                            user_id,
                            str(site["id"]),
                            SiteUpdateRequest(
                                expected_version=1,
                                description="用户刚刚填写的说明",
                                favicon_url="https://assets.example/user-icon.png",
                            ),
                        )
                    assert edited.version == 2
                    release_fetch.set()
                    assert await asyncio.wait_for(task, timeout=5) is not None
            finally:
                release_fetch.set()
                await database.dispose()

        asyncio.run(scenario())

        stored = client.get(f"/api/library/sites/{site['id']}")
        assert stored.status_code == 200
        body = stored.json()
        assert body["description"] == "用户刚刚填写的说明"
        assert body["favicon_url"] == "https://assets.example/user-icon.png"
        assert body["preview_url"] is None
        assert body["analysis_status"] == "complete"
        assert body["version"] == 2


def test_in_flight_analysis_respects_fields_the_user_explicitly_clears(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "webhub.library.routes.ingestion_worker.schedule_analysis",
        lambda *_args, **_kwargs: AnalysisSchedule(queued=0, already_queued=0, rejected=0),
    )

    with _client(tmp_path) as client:
        site = _site(
            client,
            description="准备清空的说明",
            favicon_url="https://assets.example/old-icon.png",
        )
        with sqlite3.connect(tmp_path / "main.sqlite3") as connection:
            user_id = str(
                connection.execute(
                    "SELECT user_id FROM sites WHERE id = ?", (site["id"],)
                ).fetchone()[0]
            )

        async def scenario() -> None:
            fetch_started = asyncio.Event()
            release_fetch = asyncio.Event()

            async def delayed_fetch(_url: str, *, timeout_seconds: int) -> FetchOutcome:
                assert timeout_seconds > 0
                fetch_started.set()
                await asyncio.wait_for(release_fetch.wait(), timeout=5)
                return FetchOutcome(
                    status="complete",
                    reason="ok",
                    metadata=SiteMetadata(
                        description="抓来的描述",
                        icon_url="https://example.com/favicon.ico",
                        image_url="https://example.com/preview.png",
                    ),
                )

            monkeypatch.setattr(ingestion_service, "fetch_site_metadata", delayed_fetch)
            database = Database(
                f"sqlite+aiosqlite:///{(tmp_path / 'main.sqlite3').as_posix()}"
            )
            try:
                async with database.sessions() as analysis_session:
                    task = asyncio.create_task(
                        ingestion_service.analyze_site(
                            analysis_session,
                            user_id,
                            str(site["id"]),
                        )
                    )
                    await asyncio.wait_for(fetch_started.wait(), timeout=5)
                    async with database.sessions() as edit_session:
                        edited = await library_service.update_site(
                            edit_session,
                            user_id,
                            str(site["id"]),
                            SiteUpdateRequest(
                                expected_version=1,
                                description="",
                                favicon_url=None,
                            ),
                        )
                    assert edited.version == 2
                    release_fetch.set()
                    assert await asyncio.wait_for(task, timeout=5) is not None
            finally:
                release_fetch.set()
                await database.dispose()

        asyncio.run(scenario())

        stored = client.get(f"/api/library/sites/{site['id']}")
        assert stored.status_code == 200
        body = stored.json()
        assert body["description"] == ""
        assert body["favicon_url"] is None
        assert body["preview_url"] is None
        assert body["analysis_status"] == "complete"
        assert body["version"] == 2


def test_in_flight_analysis_discards_metadata_when_the_url_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "webhub.library.routes.ingestion_worker.schedule_analysis",
        lambda *_args, **_kwargs: AnalysisSchedule(queued=0, already_queued=0, rejected=0),
    )

    with _client(tmp_path) as client:
        site = _site(client)
        with sqlite3.connect(tmp_path / "main.sqlite3") as connection:
            user_id = str(
                connection.execute(
                    "SELECT user_id FROM sites WHERE id = ?", (site["id"],)
                ).fetchone()[0]
            )

        async def scenario() -> None:
            fetch_started = asyncio.Event()
            release_fetch = asyncio.Event()

            async def delayed_fetch(_url: str, *, timeout_seconds: int) -> FetchOutcome:
                assert timeout_seconds > 0
                fetch_started.set()
                await asyncio.wait_for(release_fetch.wait(), timeout=5)
                return FetchOutcome(
                    status="complete",
                    reason="ok",
                    metadata=SiteMetadata(
                        description="旧网站的描述",
                        icon_url="https://example.com/favicon.ico",
                        image_url="https://example.com/preview.png",
                    ),
                )

            monkeypatch.setattr(ingestion_service, "fetch_site_metadata", delayed_fetch)
            database = Database(
                f"sqlite+aiosqlite:///{(tmp_path / 'main.sqlite3').as_posix()}"
            )
            try:
                async with database.sessions() as analysis_session:
                    task = asyncio.create_task(
                        ingestion_service.analyze_site(
                            analysis_session,
                            user_id,
                            str(site["id"]),
                        )
                    )
                    await asyncio.wait_for(fetch_started.wait(), timeout=5)
                    async with database.sessions() as edit_session:
                        edited = await library_service.update_site(
                            edit_session,
                            user_id,
                            str(site["id"]),
                            SiteUpdateRequest(
                                expected_version=1,
                                url="https://new.example/path",
                            ),
                        )
                    assert edited.version == 2
                    release_fetch.set()
                    assert await asyncio.wait_for(task, timeout=5) is not None
            finally:
                release_fetch.set()
                await database.dispose()

        asyncio.run(scenario())

        stored = client.get(f"/api/library/sites/{site['id']}")
        assert stored.status_code == 200
        body = stored.json()
        assert body["original_url"] == "https://new.example/path"
        assert body["description"] == ""
        assert body["favicon_url"] is None
        assert body["preview_url"] is None
        assert body["analysis_status"] == "not_analyzed"
        assert body["version"] == 2


def test_older_analysis_claim_cannot_overwrite_a_newer_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "webhub.library.routes.ingestion_worker.schedule_analysis",
        lambda *_args, **_kwargs: AnalysisSchedule(queued=0, already_queued=0, rejected=0),
    )
    with _client(tmp_path) as client:
        site = _site(client)
        with sqlite3.connect(tmp_path / "main.sqlite3") as connection:
            user_id = str(
                connection.execute(
                    "SELECT user_id FROM sites WHERE id = ?", (site["id"],)
                ).fetchone()[0]
            )

        async def scenario() -> None:
            first_started = asyncio.Event()
            release_first = asyncio.Event()
            call_count = 0

            async def ordered_fetch(_url: str, *, timeout_seconds: int) -> FetchOutcome:
                nonlocal call_count
                assert timeout_seconds > 0
                call_count += 1
                if call_count == 1:
                    first_started.set()
                    await release_first.wait()
                    return FetchOutcome(status="complete", reason="old")
                return FetchOutcome(status="failed", reason="new")

            monkeypatch.setattr(ingestion_service, "fetch_site_metadata", ordered_fetch)
            database = Database(
                f"sqlite+aiosqlite:///{(tmp_path / 'main.sqlite3').as_posix()}"
            )
            try:
                async with (
                    database.sessions() as first_session,
                    database.sessions() as second_session,
                ):
                    first = asyncio.create_task(
                        ingestion_service.analyze_site(first_session, user_id, str(site["id"]))
                    )
                    await first_started.wait()
                    await ingestion_service.analyze_site(second_session, user_id, str(site["id"]))
                    release_first.set()
                    await first
            finally:
                release_first.set()
                await database.dispose()

        asyncio.run(scenario())
        stored = client.get(f"/api/library/sites/{site['id']}").json()
        assert stored["analysis_status"] == "failed"


def test_worker_shutdown_releases_an_in_flight_pending_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_schedule = ingestion_worker.schedule_analysis
    monkeypatch.setattr(
        "webhub.library.routes.ingestion_worker.schedule_analysis",
        lambda *_args, **_kwargs: AnalysisSchedule(queued=0, already_queued=0, rejected=0),
    )
    with _client(tmp_path) as client:
        site = _site(client)
        monkeypatch.setattr(ingestion_worker, "schedule_analysis", real_schedule)
        with sqlite3.connect(tmp_path / "main.sqlite3") as connection:
            user_id = str(
                connection.execute(
                    "SELECT user_id FROM sites WHERE id = ?", (site["id"],)
                ).fetchone()[0]
            )

        async def scenario() -> None:
            started = asyncio.Event()

            async def stalled_fetch(_url: str, *, timeout_seconds: int) -> FetchOutcome:
                assert timeout_seconds > 0
                started.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

            monkeypatch.setattr(ingestion_service, "fetch_site_metadata", stalled_fetch)
            database = Database(
                f"sqlite+aiosqlite:///{(tmp_path / 'main.sqlite3').as_posix()}"
            )
            ingestion_worker.start(database)
            try:
                ingestion_worker.schedule_analysis(
                    database,
                    user_id=user_id,
                    site_ids=(str(site["id"]),),
                )
                await asyncio.wait_for(started.wait(), timeout=5)
                await asyncio.wait_for(ingestion_worker.shutdown(database), timeout=5)
            finally:
                await database.dispose()

        asyncio.run(scenario())
        stored = client.get(f"/api/library/sites/{site['id']}").json()
        assert stored["analysis_status"] == "not_analyzed"


def test_explicit_backfill_recovers_stale_pending_but_not_active_or_terminal_sites(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "webhub.library.routes.ingestion_worker.schedule_analysis",
        lambda *_args, **_kwargs: AnalysisSchedule(queued=0, already_queued=0, rejected=0),
    )
    with _client(tmp_path) as client:
        sites: list[dict[str, object]] = []
        for index in range(5):
            created = client.post(
                "/api/library/sites",
                json={"name": f"Site {index}", "url": f"https://site-{index}.example.com"},
                headers=ORIGIN,
            )
            assert created.status_code == 201
            sites.append(created.json())

        statuses = ("not_analyzed", "pending", "pending", "failed", "limited")
        with sqlite3.connect(tmp_path / "main.sqlite3") as connection:
            for site, analysis_status in zip(sites, statuses, strict=True):
                connection.execute(
                    "UPDATE sites SET analysis_status = ? WHERE id = ?",
                    (analysis_status, site["id"]),
                )
            connection.commit()

        active = {str(sites[2]["id"])}
        captured: list[tuple[str, ...]] = []

        def pending(_database: object, _user_id: str) -> frozenset[str]:
            return frozenset(active)

        def schedule(_database: object, *, user_id: str, site_ids: tuple[str, ...]):
            assert user_id
            captured.append(site_ids)
            active.update(site_ids)
            return AnalysisSchedule(queued=len(site_ids), already_queued=0, rejected=0)

        monkeypatch.setattr(
            "webhub.library.routes.ingestion_worker.pending_site_ids",
            pending,
        )
        monkeypatch.setattr(
            "webhub.library.routes.ingestion_worker.schedule_analysis",
            schedule,
        )

        response = client.post(
            "/api/library/sites/analyze-missing?limit=5000",
            headers=ORIGIN,
        )
        assert response.status_code == 202, response.text
        assert response.json() == {
            "queued_count": 2,
            "active_count": 3,
            "remaining_count": 0,
        }
        assert set(captured[0]) == {str(sites[0]["id"]), str(sites[1]["id"])}
