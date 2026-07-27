from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient

from webhub.config import Settings
from webhub.db.migrations import upgrade_database
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


def _category(client: TestClient, name: str = "工具") -> str:
    created = client.post("/api/library/categories", json={"name": name}, headers=ORIGIN)
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


def _sites(client: TestClient, category_id: str, count: int, *, prefix: str = "s") -> list[str]:
    """identity_url 在**账号内**唯一（不是分类内），所以不同分类要用不同网址。"""

    ids: list[str] = []
    for index in range(count):
        created = client.post(
            "/api/library/sites",
            json={
                "name": f"站点{prefix}{index}",
                "url": f"https://example.com/{prefix}/{index}",
                "category_id": category_id,
            },
            headers=ORIGIN,
        )
        assert created.status_code == 201, created.text
        ids.append(str(created.json()["id"]))
    return ids


def _custom_order(client: TestClient, category_id: str) -> list[str]:
    listing = client.get(
        "/api/library/sites",
        params={"category_id": category_id, "sort": "custom", "direction": "asc", "limit": 100},
    )
    assert listing.status_code == 200, listing.text
    return [item["id"] for item in listing.json()["items"]]


def _positions(database_path: Path) -> list[tuple[str, int]]:
    with sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True) as connection:
        return list(
            connection.execute("SELECT id, position FROM sites ORDER BY category_id, position")
        )


def test_new_sites_land_at_the_end_in_creation_order(tmp_path: Path) -> None:
    with _client(tmp_path) as (client, _):
        category_id = _category(client)
        ids = _sites(client, category_id, 4)
        assert _custom_order(client, category_id) == ids


def test_moving_a_site_to_the_front_survives_a_reload(tmp_path: Path) -> None:
    """The queue's acceptance case: order is stored, not just displayed."""

    with _client(tmp_path) as (client, _):
        category_id = _category(client)
        ids = _sites(client, category_id, 4)

        moved = client.post(
            f"/api/library/categories/{category_id}/reorder",
            json={"ordered_site_ids": [ids[3]], "before_site_id": ids[0]},
            headers=ORIGIN,
        )
        assert moved.status_code == 204, moved.text
        assert _custom_order(client, category_id) == [ids[3], ids[0], ids[1], ids[2]]


def test_a_block_of_sites_moves_together_in_one_request(tmp_path: Path) -> None:
    with _client(tmp_path) as (client, _):
        category_id = _category(client)
        ids = _sites(client, category_id, 5)

        moved = client.post(
            f"/api/library/categories/{category_id}/reorder",
            json={"ordered_site_ids": [ids[0], ids[1]], "before_site_id": ids[4]},
            headers=ORIGIN,
        )
        assert moved.status_code == 204, moved.text
        # Relative order inside the moved block is preserved.
        assert _custom_order(client, category_id) == [ids[2], ids[3], ids[0], ids[1], ids[4]]


def test_moving_to_the_end_uses_a_null_anchor(tmp_path: Path) -> None:
    with _client(tmp_path) as (client, _):
        category_id = _category(client)
        ids = _sites(client, category_id, 3)
        moved = client.post(
            f"/api/library/categories/{category_id}/reorder",
            json={"ordered_site_ids": [ids[0]], "before_site_id": None},
            headers=ORIGIN,
        )
        assert moved.status_code == 204
        assert _custom_order(client, category_id) == [ids[1], ids[2], ids[0]]


def test_positions_stay_unique_and_dense_after_reordering(tmp_path: Path) -> None:
    """The unique index is the real guarantee; this checks we never fight it."""

    with _client(tmp_path) as (client, database_path):
        category_id = _category(client)
        ids = _sites(client, category_id, 6)
        for anchor in (ids[0], ids[3], None):
            assert (
                client.post(
                    f"/api/library/categories/{category_id}/reorder",
                    json={"ordered_site_ids": [ids[5], ids[2]], "before_site_id": anchor},
                    headers=ORIGIN,
                ).status_code
                == 204
            )

        positions = [position for _, position in _positions(database_path)]
        assert sorted(positions) == list(range(len(ids)))


def test_reorder_rejects_ids_outside_the_category(tmp_path: Path) -> None:
    with _client(tmp_path) as (client, _):
        first = _category(client, "工具")
        second = _category(client, "阅读")
        tools = _sites(client, first, 2, prefix="tool")
        readings = _sites(client, second, 1, prefix="read")

        stray = client.post(
            f"/api/library/categories/{first}/reorder",
            json={"ordered_site_ids": [readings[0]], "before_site_id": tools[0]},
            headers=ORIGIN,
        )
        assert stray.status_code == 404
        # The category it does belong to is untouched.
        assert _custom_order(client, first) == tools


def test_reorder_rejects_duplicates_and_a_self_anchor(tmp_path: Path) -> None:
    with _client(tmp_path) as (client, _):
        category_id = _category(client)
        ids = _sites(client, category_id, 3)

        duplicated = client.post(
            f"/api/library/categories/{category_id}/reorder",
            json={"ordered_site_ids": [ids[0], ids[0]], "before_site_id": ids[2]},
            headers=ORIGIN,
        )
        assert duplicated.status_code == 422

        self_anchored = client.post(
            f"/api/library/categories/{category_id}/reorder",
            json={"ordered_site_ids": [ids[0]], "before_site_id": ids[0]},
            headers=ORIGIN,
        )
        assert self_anchored.status_code == 422
        assert _custom_order(client, category_id) == ids


def test_reorder_is_account_scoped_and_origin_checked(tmp_path: Path) -> None:
    with _client(tmp_path) as (client, _):
        category_id = _category(client)
        ids = _sites(client, category_id, 2)
        body = {"ordered_site_ids": [ids[1]], "before_site_id": ids[0]}

        assert (
            client.post(f"/api/library/categories/{category_id}/reorder", json=body).status_code
            == 403
        )

        client.cookies.clear()
        assert (
            client.post(
                "/api/auth/register",
                json={"username": "bob", "password": "another secure password here"},
                headers=ORIGIN,
            ).status_code
            == 201
        )
        assert (
            client.post(
                f"/api/library/categories/{category_id}/reorder", json=body, headers=ORIGIN
            ).status_code
            == 404
        )


def test_custom_sort_paginates_without_losing_or_repeating_rows(tmp_path: Path) -> None:
    with _client(tmp_path) as (client, _):
        category_id = _category(client)
        ids = _sites(client, category_id, 7)

        collected: list[str] = []
        cursor: str | None = None
        while True:
            params: dict[str, object] = {
                "category_id": category_id,
                "sort": "custom",
                "direction": "asc",
                "limit": 3,
            }
            if cursor:
                params["cursor"] = cursor
            page = client.get("/api/library/sites", params=params)
            assert page.status_code == 200, page.text
            body = page.json()
            collected.extend(item["id"] for item in body["items"])
            cursor = body["next_cursor"]
            if not cursor:
                break
        assert collected == ids
