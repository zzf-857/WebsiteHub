from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from webhub.config import Settings
from webhub.db.migrations import upgrade_database
from webhub.main import create_app

ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture
def spaces_client(tmp_path: Path) -> Iterator[TestClient]:
    database_path = tmp_path / "main.sqlite3"
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        data_directory=tmp_path,
    )
    upgrade_database(settings.database_url)
    with TestClient(create_app(settings=settings)) as client:
        yield client


def _register(client: TestClient, username: str = "alice") -> None:
    response = client.post(
        "/api/auth/register",
        json={"username": username, "password": "a sufficiently secure password"},
        headers=ORIGIN,
    )
    assert response.status_code == 201, response.text


def _space(client: TestClient, name: str) -> dict[str, object]:
    response = client.post("/api/spaces", json={"name": name}, headers=ORIGIN)
    assert response.status_code == 201, response.text
    return response.json()


def _site(client: TestClient, name: str) -> dict[str, object]:
    response = client.post(
        "/api/library/sites",
        json={"name": name, "url": f"https://{name.casefold()}.example.com"},
        headers=ORIGIN,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _add_member(
    client: TestClient,
    space_id: object,
    site_id: object,
    expected_version: int,
) -> dict[str, object]:
    response = client.post(
        f"/api/spaces/{space_id}/members",
        json={"expected_version": expected_version, "site_id": site_id},
        headers=ORIGIN,
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_space_crud_uses_bounded_stable_pagination_and_versions(
    spaces_client: TestClient,
) -> None:
    client = spaces_client
    assert client.get("/api/spaces").status_code == 401
    assert client.post("/api/spaces", json={"name": "Private"}, headers=ORIGIN).status_code == 401

    _register(client)
    assert client.post("/api/spaces", json={"name": "Private"}).status_code == 403
    assert (
        client.post(
            "/api/spaces",
            json={"name": "Private"},
            headers={"Origin": "http://attacker.invalid"},
        ).status_code
        == 403
    )

    charlie = _space(client, "Charlie")
    alpha = _space(client, "Alpha")
    bravo = _space(client, "Bravo")
    assert charlie["version"] == alpha["version"] == bravo["version"] == 1

    first_page = client.get(
        "/api/spaces",
        params={"sort": "name", "direction": "asc", "limit": 2},
    )
    assert first_page.status_code == 200, first_page.text
    first_payload = first_page.json()
    assert [item["name"] for item in first_payload["items"]] == ["Alpha", "Bravo"]
    assert first_payload["aggregate"] == {"total_count": 3}
    assert first_payload["next_cursor"]

    second_page = client.get(
        "/api/spaces",
        params={
            "sort": "name",
            "direction": "asc",
            "limit": 2,
            "cursor": first_payload["next_cursor"],
        },
    )
    assert [item["name"] for item in second_page.json()["items"]] == ["Charlie"]
    assert second_page.json()["next_cursor"] is None
    invalid_cursor = client.get("/api/spaces", params={"cursor": "bad"})
    assert invalid_cursor.status_code == 422
    assert invalid_cursor.json()["detail"]["code"] == "validation_error"
    for mismatched_scope in (
        {"sort": "name", "direction": "desc"},
        {"sort": "updated", "direction": "asc"},
    ):
        response = client.get(
            "/api/spaces",
            params={
                **mismatched_scope,
                "limit": 2,
                "cursor": first_payload["next_cursor"],
            },
        )
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "validation_error"
    assert client.get("/api/spaces", params={"limit": 101}).status_code == 422
    assert client.get(f"/api/spaces/{alpha['id']}", params={"limit": 101}).status_code == 422

    duplicate = client.post("/api/spaces", json={"name": "  Ａｌｐｈａ  "}, headers=ORIGIN)
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == {
        "code": "duplicate_name",
        "message": "Space 名称已存在",
    }

    renamed = client.patch(
        f"/api/spaces/{alpha['id']}",
        json={"expected_version": 1, "name": "  Product   launch  "},
        headers=ORIGIN,
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "Product launch"
    assert renamed.json()["version"] == 2

    stale = client.patch(
        f"/api/spaces/{alpha['id']}",
        json={"expected_version": 1, "name": "Stale"},
        headers=ORIGIN,
    )
    assert stale.status_code == 409
    assert stale.json()["detail"] == {
        "code": "version_conflict",
        "message": "Space 已被修改，请刷新后重试",
    }

    duplicate_rename = client.patch(
        f"/api/spaces/{bravo['id']}",
        json={"expected_version": 1, "name": "Product launch"},
        headers=ORIGIN,
    )
    assert duplicate_rename.status_code == 409
    assert duplicate_rename.json()["detail"]["code"] == "duplicate_name"
    unchanged = client.get(f"/api/spaces/{bravo['id']}").json()
    assert unchanged["name"] == "Bravo"
    assert unchanged["version"] == 1

    missing = client.get("/api/spaces/missing-space")
    assert missing.status_code == 404
    assert missing.json()["detail"] == {
        "code": "not_found",
        "message": "Space 不存在",
    }
    assert (
        client.patch(
            f"/api/spaces/{alpha['id']}",
            json={"expected_version": 2, "name": "No origin"},
        ).status_code
        == 403
    )


def test_members_support_multi_space_paging_reorder_and_non_destructive_delete(
    spaces_client: TestClient,
) -> None:
    client = spaces_client
    _register(client)
    alpha = _site(client, "Alpha")
    bravo = _site(client, "Bravo")
    charlie = _site(client, "Charlie")
    delta = _site(client, "Delta")
    primary = _space(client, "Primary")
    secondary = _space(client, "Secondary")

    primary_state = _add_member(client, primary["id"], alpha["id"], 1)
    primary_state = _add_member(
        client, primary["id"], bravo["id"], primary_state["space"]["version"]
    )
    primary_state = _add_member(
        client, primary["id"], charlie["id"], primary_state["space"]["version"]
    )
    secondary_state = _add_member(client, secondary["id"], alpha["id"], 1)
    assert primary_state["space"]["version"] == 4
    assert secondary_state["space"]["member_count"] == 1

    filtered_library = client.get("/api/library/sites", params={"space_id": primary["id"]})
    assert filtered_library.status_code == 200, filtered_library.text
    assert filtered_library.json()["aggregate"]["matched_count"] == 3
    assert {item["id"] for item in filtered_library.json()["items"]} == {
        alpha["id"],
        bravo["id"],
        charlie["id"],
    }
    primary_site_page = client.get(
        "/api/library/sites",
        params={
            "space_id": primary["id"],
            "sort": "name",
            "direction": "asc",
            "limit": 1,
        },
    ).json()
    wrong_space_cursor = client.get(
        "/api/library/sites",
        params={
            "space_id": secondary["id"],
            "sort": "name",
            "direction": "asc",
            "limit": 1,
            "cursor": primary_site_page["next_cursor"],
        },
    )
    assert wrong_space_cursor.status_code == 422

    duplicate = client.post(
        f"/api/spaces/{primary['id']}/members",
        json={"expected_version": 4, "site_id": alpha["id"]},
        headers=ORIGIN,
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == {
        "code": "member_exists",
        "message": "网站已在该 Space 中",
    }

    stale_add = client.post(
        f"/api/spaces/{primary['id']}/members",
        json={"expected_version": 3, "site_id": delta["id"]},
        headers=ORIGIN,
    )
    assert stale_add.status_code == 409
    assert stale_add.json()["detail"]["code"] == "version_conflict"

    first_page = client.get(f"/api/spaces/{primary['id']}", params={"limit": 2}).json()
    assert [member["site"]["name"] for member in first_page["members"]] == [
        "Alpha",
        "Bravo",
    ]
    assert [member["position"] for member in first_page["members"]] == [0, 1]
    assert first_page["member_count"] == 3
    old_cursor = first_page["next_cursor"]
    assert old_cursor
    second_page = client.get(
        f"/api/spaces/{primary['id']}",
        params={"limit": 2, "cursor": old_cursor},
    ).json()
    assert [member["site"]["name"] for member in second_page["members"]] == ["Charlie"]
    wrong_space_cursor = client.get(
        f"/api/spaces/{secondary['id']}",
        params={"limit": 2, "cursor": old_cursor},
    )
    assert wrong_space_cursor.status_code == 422
    assert wrong_space_cursor.json()["detail"]["code"] == "validation_error"

    no_origin = client.patch(
        f"/api/spaces/{primary['id']}/members/order",
        json={
            "expected_version": 4,
            "ordered_site_ids": [charlie["id"]],
            "before_site_id": alpha["id"],
        },
    )
    assert no_origin.status_code == 403
    reordered = client.patch(
        f"/api/spaces/{primary['id']}/members/order",
        json={
            "expected_version": 4,
            "ordered_site_ids": [charlie["id"], alpha["id"]],
            "before_site_id": bravo["id"],
        },
        headers=ORIGIN,
    )
    assert reordered.status_code == 200, reordered.text
    assert reordered.json()["version"] == 5

    invalidated_library_page = client.get(
        "/api/library/sites",
        params={
            "space_id": primary["id"],
            "sort": "name",
            "direction": "asc",
            "limit": 1,
            "cursor": primary_site_page["next_cursor"],
        },
    )
    assert invalidated_library_page.status_code == 422

    invalidated_page = client.get(
        f"/api/spaces/{primary['id']}",
        params={"limit": 2, "cursor": old_cursor},
    )
    assert invalidated_page.status_code == 422
    assert invalidated_page.json()["detail"]["code"] == "validation_error"
    ordered = client.get(f"/api/spaces/{primary['id']}").json()
    assert [member["site"]["name"] for member in ordered["members"]] == [
        "Charlie",
        "Alpha",
        "Bravo",
    ]
    assert [member["position"] for member in ordered["members"]] == [0, 1, 2]

    invalid_anchor = client.patch(
        f"/api/spaces/{primary['id']}/members/order",
        json={
            "expected_version": 5,
            "ordered_site_ids": [charlie["id"]],
            "before_site_id": "missing-member",
        },
        headers=ORIGIN,
    )
    assert invalid_anchor.status_code == 404
    assert invalid_anchor.json()["detail"]["code"] == "member_not_found"
    duplicate_move = client.patch(
        f"/api/spaces/{primary['id']}/members/order",
        json={
            "expected_version": 5,
            "ordered_site_ids": [charlie["id"], charlie["id"]],
        },
        headers=ORIGIN,
    )
    assert duplicate_move.status_code == 422

    stale_remove = client.delete(
        f"/api/spaces/{primary['id']}/members/{bravo['id']}",
        params={"expected_version": 4},
        headers=ORIGIN,
    )
    assert stale_remove.status_code == 409
    assert stale_remove.json()["detail"]["code"] == "version_conflict"

    removed = client.delete(
        f"/api/spaces/{primary['id']}/members/{alpha['id']}",
        params={"expected_version": 5},
        headers=ORIGIN,
    )
    assert removed.status_code == 200, removed.text
    assert removed.json()["version"] == 6
    assert removed.json()["member_count"] == 2
    missing_member = client.delete(
        f"/api/spaces/{primary['id']}/members/{alpha['id']}",
        params={"expected_version": 6},
        headers=ORIGIN,
    )
    assert missing_member.status_code == 404
    assert missing_member.json()["detail"]["code"] == "member_not_found"
    assert [
        member["site"]["id"]
        for member in client.get(f"/api/spaces/{secondary['id']}").json()["members"]
    ] == [alpha["id"]]

    preview = client.get(f"/api/spaces/{primary['id']}/delete-preview")
    assert preview.status_code == 200
    assert preview.json()["affected_site_count"] == 2
    assert preview.json()["space"]["version"] == 6
    stale_delete = client.delete(
        f"/api/spaces/{primary['id']}",
        params={"expected_version": 5},
        headers=ORIGIN,
    )
    assert stale_delete.status_code == 409
    assert stale_delete.json()["detail"]["code"] == "version_conflict"

    deleted = client.delete(
        f"/api/spaces/{primary['id']}",
        params={"expected_version": 6},
        headers=ORIGIN,
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["unlinked_site_count"] == 2
    assert client.get(f"/api/spaces/{primary['id']}").status_code == 404
    assert client.get("/api/library/sites", params={"space_id": primary["id"]}).status_code == 404
    for site in (alpha, bravo, charlie):
        assert client.get(f"/api/library/sites/{site['id']}").status_code == 200
    assert client.get(f"/api/spaces/{secondary['id']}").json()["member_count"] == 1

    removed_site = client.delete(
        f"/api/library/sites/{alpha['id']}",
        params={"expected_version": alpha["version"]},
        headers=ORIGIN,
    )
    assert removed_site.status_code == 200
    secondary_after_site_delete = client.get(f"/api/spaces/{secondary['id']}").json()
    assert secondary_after_site_delete["version"] == 3
    assert secondary_after_site_delete["member_count"] == 0
