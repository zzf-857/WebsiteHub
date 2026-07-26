from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from webhub.config import Settings
from webhub.db.migrations import upgrade_database
from webhub.main import create_app

ORIGIN = {"Origin": "http://testserver"}
_REAL_ASYNC_CLIENT = httpx.AsyncClient

PAGE = """<!doctype html><html><head>
<title>抓来的标题</title>
<meta name="description" content="抓来的描述">
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
        return httpx.Response(
            status, content=body.encode(), headers={"content-type": "text/html; charset=utf-8"}
        )

    def factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", factory)

    async def allow(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr("webhub.ingestion.fetcher.validate_connection_target", allow)


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

        analyzed = client.post(
            f"/api/library/sites/{site['id']}/analyze", headers=ORIGIN
        )
        assert analyzed.status_code == 200, analyzed.text
        body = analyzed.json()
        assert body["analysis_status"] == "complete"
        assert body["description"] == "抓来的描述"
        assert body["favicon_url"] == "https://example.com/favicon.ico"


def test_analysis_never_overwrites_what_the_user_typed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typed sentence is a decision; a meta tag is a guess about it. Guesses lose."""

    _stub_page(monkeypatch)
    with _client(tmp_path) as client:
        site = _site(client, description="我自己写的说明")

        analyzed = client.post(
            f"/api/library/sites/{site['id']}/analyze", headers=ORIGIN
        )
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
        analyzed = client.post(
            f"/api/library/sites/{site['id']}/analyze", headers=ORIGIN
        )
        assert analyzed.status_code == 200
        assert analyzed.json()["analysis_status"] == "failed"


def test_a_page_without_metadata_is_limited_not_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_page(monkeypatch, body="<html><head></head><body><div id=root></div></body></html>")
    with _client(tmp_path) as client:
        site = _site(client)
        analyzed = client.post(
            f"/api/library/sites/{site['id']}/analyze", headers=ORIGIN
        )
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
