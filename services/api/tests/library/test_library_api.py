from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from webhub.config import Settings
from webhub.db.migrations import upgrade_database
from webhub.main import create_app

COOKIE_NAME = "webhub_session"
ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture
def library_client(tmp_path: Path) -> Iterator[TestClient]:
    database_path = tmp_path / "main.sqlite3"
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        data_directory=tmp_path,
    )
    upgrade_database(settings.database_url)
    with TestClient(create_app(settings=settings)) as client:
        yield client


def _register(client: TestClient, username: str) -> str:
    response = client.post(
        "/api/auth/register",
        json={"username": username, "password": "a sufficiently secure password"},
        headers=ORIGIN,
    )
    assert response.status_code == 201, response.text
    token = response.cookies.get(COOKIE_NAME)
    assert token
    return token


def _use_token(client: TestClient, token: str) -> None:
    client.cookies.clear()
    client.cookies.set(COOKIE_NAME, token)


def _category(client: TestClient, name: str) -> dict[str, object]:
    response = client.post("/api/library/categories", json={"name": name}, headers=ORIGIN)
    assert response.status_code == 201, response.text
    return response.json()


def _tag(client: TestClient, name: str) -> dict[str, object]:
    response = client.post("/api/library/tags", json={"name": name}, headers=ORIGIN)
    assert response.status_code == 201, response.text
    return response.json()


def _site(
    client: TestClient,
    *,
    name: str,
    url: str,
    category_id: str | None = None,
    tag_ids: list[str] | None = None,
    description: str = "",
    pinned: bool = False,
) -> dict[str, object]:
    response = client.post(
        "/api/library/sites",
        json={
            "name": name,
            "url": url,
            "category_id": category_id,
            "tag_ids": tag_ids or [],
            "description": description,
            "pinned": pinned,
        },
        headers=ORIGIN,
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_library_requires_login_and_origin_for_writes(library_client: TestClient) -> None:
    client = library_client
    assert client.get("/api/library/categories").status_code == 401
    assert client.get("/api/library/tags").status_code == 401
    assert client.get("/api/library/sites").status_code == 401

    _register(client, "alice")
    categories = client.get("/api/library/categories")
    assert categories.status_code == 200
    assert categories.json()["items"][0]["name"] == "未分类"
    assert categories.json()["items"][0]["is_default"] is True

    missing_origin = client.post("/api/library/categories", json={"name": "开发"})
    untrusted_origin = client.post(
        "/api/library/categories",
        json={"name": "开发"},
        headers={"Origin": "http://attacker.invalid"},
    )
    assert missing_origin.status_code == 403
    assert untrusted_origin.status_code == 403
    assert client.get("/api/library/categories").json()["items"] == categories.json()["items"]


def test_site_crud_search_and_stale_version(library_client: TestClient) -> None:
    client = library_client
    _register(client, "alice")
    category = _category(client, "开发 文档")
    tag = _tag(client, "参考资料")
    created = _site(
        client,
        name="GitHub 文档",
        url="HTTPS://EXAMPLE.com:443/docs?q=a#one",
        category_id=str(category["id"]),
        tag_ids=[str(tag["id"])],
        description="代码托管参考手册",
        pinned=True,
    )

    assert created["identity_url"] == "https://example.com/docs?q=a#one"
    assert created["version"] == 1
    assert created["category"]["id"] == category["id"]
    assert created["tags"] == [{"id": tag["id"], "name": "参考资料"}]

    short_search = client.get("/api/library/sites", params={"q": "开"})
    fts_name = client.get("/api/library/sites", params={"q": "GitHub"})
    fts_description = client.get("/api/library/sites", params={"q": "代码托管"})
    fts_tag = client.get("/api/library/sites", params={"q": "参考资料"})
    mixed_fields = client.get("/api/library/sites", params={"q": "GitHub 开发 参考资料"})
    normalized_query = client.get("/api/library/sites", params={"q": "ＧｉｔＨｕｂ"})
    assert short_search.json()["aggregate"]["matched_count"] == 1
    assert fts_name.json()["aggregate"]["matched_count"] == 1
    assert fts_description.json()["aggregate"]["matched_count"] == 1
    assert fts_tag.json()["aggregate"]["matched_count"] == 1
    assert mixed_fields.json()["aggregate"]["matched_count"] == 1
    assert normalized_query.json()["aggregate"]["matched_count"] == 1

    updated = client.patch(
        f"/api/library/sites/{created['id']}",
        json={
            "expected_version": 1,
            "name": "GitHub API",
            "url": "https://example.com/docs?q=a#two",
            "description": "更新后的接口文档",
            "tag_ids": [],
            "pinned": False,
        },
        headers=ORIGIN,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["version"] == 2
    assert updated.json()["tags"] == []
    assert updated.json()["identity_url"].endswith("?q=a#two")
    assert client.get("/api/library/sites", params={"q": "更新后的接口文档"}).json()[
        "aggregate"
    ]["matched_count"] == 1
    assert client.get("/api/library/sites", params={"q": "参考资料"}).json()["aggregate"][
        "matched_count"
    ] == 0

    stale = client.patch(
        f"/api/library/sites/{created['id']}",
        json={"expected_version": 1, "pinned": True},
        headers=ORIGIN,
    )
    assert stale.status_code == 409
    assert stale.json()["detail"] == {
        "code": "version_conflict",
        "message": "网站已被修改，请刷新后重试",
    }

    missing_version = client.delete(f"/api/library/sites/{created['id']}", headers=ORIGIN)
    assert missing_version.status_code == 422
    deleted = client.delete(
        f"/api/library/sites/{created['id']}",
        params={"expected_version": updated.json()["version"]},
        headers=ORIGIN,
    )
    assert deleted.status_code == 200
    missing = client.get(f"/api/library/sites/{created['id']}")
    assert missing.status_code == 404
    assert missing.json()["detail"] == {"code": "not_found", "message": "网站不存在"}


def test_url_identity_is_strict_per_account(library_client: TestClient) -> None:
    client = library_client
    alice_token = _register(client, "alice")
    variants = (
        "https://example.com/docs?q=a#one",
        "https://example.com/docs?q=a#two",
        "https://example.com/docs?q=b#one",
        "https://example.com/docs?a=1&b=2",
        "https://example.com/docs?b=2&a=1",
    )
    created = [
        _site(client, name=f"Variant {index}", url=url)
        for index, url in enumerate(variants)
    ]
    assert len({item["identity_url"] for item in created}) == len(variants)

    duplicate = client.post(
        "/api/library/sites",
        json={
            "name": "Duplicate",
            "url": "HTTPS://EXAMPLE.COM:443/docs?q=a#one",
            "tag_ids": [],
        },
        headers=ORIGIN,
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == {
        "code": "duplicate_url",
        "message": "该网址已存在于当前账号的资料库",
    }

    client.cookies.clear()
    _register(client, "bob")
    same_other_account = _site(client, name="Bob copy", url=variants[0])
    assert same_other_account["identity_url"] == created[0]["identity_url"]
    _use_token(client, alice_token)
    assert client.get("/api/library/sites").json()["aggregate"]["matched_count"] == 5


def test_cursor_pagination_sort_filter_and_scope_binding(library_client: TestClient) -> None:
    client = library_client
    _register(client, "alice")
    category = _category(client, "开发")
    other_category = _category(client, "日常")
    tag = _tag(client, "API")
    for name, pinned in (
        ("Alpha", True),
        ("Bravo", False),
        ("Charlie", True),
        ("Delta", False),
        ("Echo", False),
    ):
        _site(
            client,
            name=name,
            url=f"https://{name.casefold()}.example.com",
            category_id=str(category["id"]),
            tag_ids=[str(tag["id"])],
            pinned=pinned,
        )
    _site(
        client,
        name="Outside",
        url="https://outside.example.com",
        category_id=str(other_category["id"]),
    )

    names: list[str] = []
    cursor = None
    for _ in range(3):
        page = client.get(
            "/api/library/sites",
            params={
                "category_id": category["id"],
                "sort": "name",
                "direction": "asc",
                "limit": 2,
                **({"cursor": cursor} if cursor else {}),
            },
        )
        assert page.status_code == 200, page.text
        payload = page.json()
        names.extend(item["name"] for item in payload["items"])
        assert payload["aggregate"] == {"matched_count": 5, "pinned_count": 2}
        cursor = payload["next_cursor"]
    assert names == ["Alpha", "Bravo", "Charlie", "Delta", "Echo"]
    assert cursor is None

    filtered = client.get(
        "/api/library/sites",
        params={"tag_id": tag["id"], "pinned": True},
    )
    assert filtered.json()["aggregate"] == {"matched_count": 2, "pinned_count": 2}

    first_page = client.get(
        "/api/library/sites",
        params={"category_id": category["id"], "limit": 1},
    ).json()
    wrong_scope = client.get(
        "/api/library/sites",
        params={
            "category_id": other_category["id"],
            "limit": 1,
            "cursor": first_page["next_cursor"],
        },
    )
    assert wrong_scope.status_code == 422
    assert client.get("/api/library/sites", params={"cursor": "not-a-cursor"}).status_code == 422
    assert client.get("/api/library/sites", params={"limit": 101}).status_code == 422


def test_category_preview_delete_and_tag_delete_preserve_sites(
    library_client: TestClient,
) -> None:
    client = library_client
    _register(client, "alice")
    category = _category(client, "稍后整理")
    tag = _tag(client, "临时")
    first = _site(
        client,
        name="One",
        url="https://one.example.com",
        category_id=str(category["id"]),
        tag_ids=[str(tag["id"])],
    )
    second = _site(
        client,
        name="Two",
        url="https://two.example.com",
        category_id=str(category["id"]),
        tag_ids=[str(tag["id"])],
    )

    preview = client.get(f"/api/library/categories/{category['id']}/delete-preview")
    assert preview.status_code == 200
    assert preview.json()["affected_site_count"] == 2
    assert preview.json()["replacement_category"]["name"] == "未分类"
    assert preview.json()["replacement_category"]["is_default"] is True

    removed_category = client.delete(
        f"/api/library/categories/{category['id']}", headers=ORIGIN
    )
    assert removed_category.json()["reassigned_site_count"] == 2
    for site_id in (first["id"], second["id"]):
        restored = client.get(f"/api/library/sites/{site_id}")
        assert restored.status_code == 200
        assert restored.json()["category"]["is_default"] is True
        assert restored.json()["version"] == 2

    removed_tag = client.delete(f"/api/library/tags/{tag['id']}", headers=ORIGIN)
    assert removed_tag.json()["unlinked_site_count"] == 2
    for site_id in (first["id"], second["id"]):
        restored = client.get(f"/api/library/sites/{site_id}")
        assert restored.status_code == 200
        assert restored.json()["tags"] == []
        assert restored.json()["version"] == 3

    default_category = client.get("/api/library/categories").json()["items"][0]
    cannot_delete_default = client.delete(
        f"/api/library/categories/{default_category['id']}", headers=ORIGIN
    )
    assert cannot_delete_default.status_code == 409


def test_normalized_names_and_invalid_urls_return_clear_errors(
    library_client: TestClient,
) -> None:
    client = library_client
    _register(client, "alice")
    _category(client, "  Development   Tools  ")
    duplicate_category = client.post(
        "/api/library/categories",
        json={"name": "development tools"},
        headers=ORIGIN,
    )
    assert duplicate_category.status_code == 409
    other_category = _category(client, "Other category")
    duplicate_category_rename = client.patch(
        f"/api/library/categories/{other_category['id']}",
        json={"name": "development tools"},
        headers=ORIGIN,
    )
    assert duplicate_category_rename.status_code == 409

    _tag(client, "ＡＰＩ")
    duplicate_tag = client.post(
        "/api/library/tags", json={"name": "api"}, headers=ORIGIN
    )
    assert duplicate_tag.status_code == 409
    other_tag = _tag(client, "Other tag")
    duplicate_tag_rename = client.patch(
        f"/api/library/tags/{other_tag['id']}",
        json={"name": "api"},
        headers=ORIGIN,
    )
    assert duplicate_tag_rename.status_code == 409

    invalid = client.post(
        "/api/library/sites",
        json={"name": "Local file", "url": "file:///tmp/private", "tag_ids": []},
        headers=ORIGIN,
    )
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "validation_error"
    assert invalid.json()["detail"]["message"].startswith("网址无效或不受支持")


def test_site_update_conflicts_remain_mapped_when_relationships_are_replaced(
    library_client: TestClient,
) -> None:
    client = library_client
    _register(client, "alice")
    tag = _tag(client, "replacement")
    first = _site(client, name="First", url="https://first.example.com")
    second = _site(client, name="Second", url="https://second.example.com")

    duplicate = client.patch(
        f"/api/library/sites/{second['id']}",
        json={
            "expected_version": second["version"],
            "url": first["identity_url"],
            "tag_ids": [tag["id"]],
        },
        headers=ORIGIN,
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == {
        "code": "duplicate_url",
        "message": "该网址已存在于当前账号的资料库",
    }

    unchanged = client.get(f"/api/library/sites/{second['id']}")
    assert unchanged.status_code == 200
    assert unchanged.json()["version"] == second["version"]
    assert unchanged.json()["tags"] == []

    empty_patch = client.patch(
        f"/api/library/sites/{second['id']}",
        json={"expected_version": second["version"]},
        headers=ORIGIN,
    )
    assert empty_patch.status_code == 422


def test_favicon_url_is_normalized_and_limited_to_absolute_http_urls(
    library_client: TestClient,
) -> None:
    client = library_client
    _register(client, "alice")

    blank = client.post(
        "/api/library/sites",
        json={
            "name": "Blank favicon",
            "url": "https://blank-favicon.example.com",
            "favicon_url": "   ",
        },
        headers=ORIGIN,
    )
    assert blank.status_code == 201, blank.text
    assert blank.json()["favicon_url"] is None

    created = client.post(
        "/api/library/sites",
        json={
            "name": "HTTP favicon",
            "url": "https://http-favicon.example.com",
            "favicon_url": "  HTTPS://ICONS.EXAMPLE.COM/favicon.svg  ",
        },
        headers=ORIGIN,
    )
    assert created.status_code == 201, created.text
    assert created.json()["favicon_url"] == "https://icons.example.com/favicon.svg"

    for favicon_url in (
        "/favicon.ico",
        "//icons.example.com/favicon.ico",
        "data:image/png;base64,AAAA",
        "ftp://icons.example.com/favicon.ico",
    ):
        rejected = client.patch(
            f"/api/library/sites/{created.json()['id']}",
            json={
                "expected_version": created.json()["version"],
                "favicon_url": favicon_url,
            },
            headers=ORIGIN,
        )
        assert rejected.status_code == 422

    cleared = client.patch(
        f"/api/library/sites/{created.json()['id']}",
        json={
            "expected_version": created.json()["version"],
            "favicon_url": "\t\r\n",
        },
        headers=ORIGIN,
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["favicon_url"] is None


def test_delete_site_rejects_a_stale_version_without_deleting(
    library_client: TestClient,
) -> None:
    client = library_client
    _register(client, "alice")
    created = _site(client, name="Versioned", url="https://versioned.example.com")
    updated = client.patch(
        f"/api/library/sites/{created['id']}",
        json={"expected_version": created["version"], "pinned": True},
        headers=ORIGIN,
    ).json()

    stale = client.delete(
        f"/api/library/sites/{created['id']}",
        params={"expected_version": created["version"]},
        headers=ORIGIN,
    )
    assert stale.status_code == 409
    assert stale.json()["detail"] == {
        "code": "version_conflict",
        "message": "网站已被修改，请刷新后重试",
    }
    assert client.get(f"/api/library/sites/{created['id']}").json()["version"] == updated[
        "version"
    ]

    deleted = client.delete(
        f"/api/library/sites/{created['id']}",
        params={"expected_version": updated["version"]},
        headers=ORIGIN,
    )
    assert deleted.status_code == 200, deleted.text
