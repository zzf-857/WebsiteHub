from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from webhub.config import Settings
from webhub.db.migrations import upgrade_database
from webhub.main import create_app

ORIGIN = {"Origin": "http://testserver"}
_REAL_ASYNC_CLIENT = httpx.AsyncClient

SITES = [
    ("Figma", "界面设计工具"),
    ("Figma 社区", "设计资源分享"),
    ("Notion", "笔记与协作"),
]


@pytest.fixture
def library(tmp_path: Path) -> Iterator[TestClient]:
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
        for index, (name, description) in enumerate(SITES):
            created = client.post(
                "/api/library/sites",
                json={
                    "name": name,
                    "url": f"https://site-{index}.example.com",
                    "description": description,
                },
                headers=ORIGIN,
            )
            assert created.status_code == 201, created.text
        yield client


def _watch(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": []})

    def factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(record)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return seen


def test_the_existing_sorts_are_untouched_by_the_new_one(
    library: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The criterion: without a Provider, search must behave exactly as before.

    Stated structurally rather than by luck — the four original sorts never
    enter the relevance path at all, so they cannot call a vendor.
    """

    seen = _watch(monkeypatch)
    for sort in ("created", "updated", "name", "custom"):
        response = library.get("/api/library/sites", params={"q": "Figma", "sort": sort})
        assert response.status_code == 200, response.text
        assert response.json()["aggregate"]["matched_count"] == 2
    assert seen == [], "普通排序不得触发任何厂商调用"


def test_relevance_promotes_the_exact_name_match(library: TestClient) -> None:
    """Both sites match "Figma"; the one actually named that must come first."""

    response = library.get(
        "/api/library/sites",
        params={"q": "Figma", "sort": "relevance"},
    )
    assert response.status_code == 200, response.text
    names = [item["name"] for item in response.json()["items"]]
    assert names[0] == "Figma"
    assert set(names) == {"Figma", "Figma 社区"}


def test_relevance_without_a_provider_never_calls_out(
    library: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No embedding Provider means pure keyword ordering, silently."""

    seen = _watch(monkeypatch)
    response = library.get("/api/library/sites", params={"q": "设计", "sort": "relevance"})
    assert response.status_code == 200, response.text
    assert response.json()["aggregate"]["matched_count"] == 2
    assert seen == []


def test_relevance_requires_a_query(library: TestClient) -> None:
    """Ordering by relevance to nothing is meaningless — say so, don't guess."""

    response = library.get("/api/library/sites", params={"sort": "relevance"})
    assert response.status_code == 422, response.text


def test_a_relevance_cursor_is_rejected_by_the_other_sorts(library: TestClient) -> None:
    """The two pagination schemes are different; mixing them must not silently work."""

    first = library.get(
        "/api/library/sites",
        params={"q": "Figma", "sort": "relevance", "limit": 1},
    )
    assert first.status_code == 200, first.text
    cursor = first.json()["next_cursor"]
    assert cursor, "还有第二条时必须给出游标"

    crossed = library.get(
        "/api/library/sites",
        params={"q": "Figma", "sort": "updated", "cursor": cursor},
    )
    assert crossed.status_code == 422, crossed.text


def test_relevance_paginates_without_repeating_or_dropping(library: TestClient) -> None:
    seen: list[str] = []
    cursor: str | None = None
    for _page in range(4):
        params = {"q": "Figma", "sort": "relevance", "limit": 1}
        if cursor:
            params["cursor"] = cursor
        response = library.get("/api/library/sites", params=params)
        assert response.status_code == 200, response.text
        body = response.json()
        seen.extend(item["id"] for item in body["items"])
        cursor = body["next_cursor"]
        if not cursor:
            break
    assert len(seen) == 2
    assert len(set(seen)) == 2, "翻页不得重复返回同一条"


def test_relevance_skips_provider_resolution_when_nothing_is_indexed(
    library: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolving a Provider costs a real DNS lookup; a search box must not pay it.

    With no vectors stored, semantic recall cannot contribute a single row, so
    the resolution is skipped entirely rather than made cheaper.
    """

    from webhub.library import routes as library_routes

    resolved: list[str] = []

    async def spy_resolve(_session, _settings, *, user_id: str, kind: str):
        resolved.append(f"{user_id}:{kind}")
        return None

    monkeypatch.setattr(library_routes, "resolve_optional_binding", spy_resolve)

    response = library.get("/api/library/sites", params={"q": "Figma", "sort": "relevance"})
    assert response.status_code == 200, response.text
    assert resolved == [], "库里没有任何向量时不得解析 Provider"
