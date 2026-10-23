"""Q52：标签合并（FR-OR-003）与分类图标白名单。"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from webhub.config import Settings
from webhub.db.migrations import upgrade_database
from webhub.library.icons import CATEGORY_ICON_NAMES, SEMANTIC_ICONS, infer_category_icon
from webhub.main import create_app

COOKIE_NAME = "webhub_session"
ORIGIN = {"Origin": "http://testserver"}
REPO_ROOT = Path(__file__).resolve().parents[4]


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    return tmp_path / "main.sqlite3"


@pytest.fixture
def client(tmp_path: Path, database_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        data_directory=tmp_path,
    )
    upgrade_database(settings.database_url)
    with TestClient(create_app(settings=settings)) as test_client:
        yield test_client


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


def _use(client: TestClient, token: str) -> None:
    client.cookies.clear()
    client.cookies.set(COOKIE_NAME, token)


def _tag(client: TestClient, name: str) -> dict[str, object]:
    response = client.post("/api/library/tags", json={"name": name}, headers=ORIGIN)
    assert response.status_code == 201, response.text
    return response.json()


def _site(client: TestClient, url: str, tag_ids: list[str]) -> dict[str, object]:
    response = client.post(
        "/api/library/sites",
        json={"name": url, "url": url, "tag_ids": tag_ids},
        headers=ORIGIN,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _tag_names(client: TestClient, site_id: object) -> list[str]:
    response = client.get(f"/api/library/sites/{site_id}")
    assert response.status_code == 200, response.text
    return [str(tag["name"]) for tag in response.json()["tags"]]


# ── 标签合并 ─────────────────────────────────────────────────────────────


def test_merge_preview_counts_affected_and_overlapping_sites(client: TestClient) -> None:
    _register(client, "alice")
    source = _tag(client, "LLM")
    target = _tag(client, "大模型")
    _site(client, "https://one.example.com", [str(source["id"])])
    _site(client, "https://two.example.com", [str(source["id"]), str(target["id"])])
    _site(client, "https://three.example.com", [str(target["id"])])

    preview = client.get(
        f"/api/library/tags/{source['id']}/merge-preview",
        params={"target_id": target["id"]},
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["affected_site_count"] == 2
    assert body["already_tagged_count"] == 1
    assert body["source"]["name"] == "LLM"
    assert body["source"]["site_count"] == 2
    assert body["target"]["name"] == "大模型"
    assert body["target"]["site_count"] == 2
    # 预览不改任何东西
    assert {tag["name"] for tag in client.get("/api/library/tags").json()["items"]} == {
        "LLM",
        "大模型",
    }


def test_merge_moves_links_deletes_source_and_bumps_versions(
    client: TestClient, database_path: Path
) -> None:
    _register(client, "alice")
    source = _tag(client, "LLM")
    target = _tag(client, "大模型")
    other = _tag(client, "其它")
    only_source = _site(client, "https://one.example.com", [str(source["id"]), str(other["id"])])
    both = _site(client, "https://two.example.com", [str(source["id"]), str(target["id"])])
    only_target = _site(client, "https://three.example.com", [str(target["id"])])
    # 模拟两种起点：一条还没有偏好记录，一条的标签来自自动分析
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "DELETE FROM site_metadata_preferences WHERE site_id = ?", (only_source["id"],)
        )
        connection.execute(
            "UPDATE site_metadata_preferences SET tags_are_manual = 0, tags_are_llm = 1 "
            "WHERE site_id = ?",
            (both["id"],),
        )

    merged = client.post(
        f"/api/library/tags/{source['id']}/merge",
        json={"target_id": target["id"]},
        headers=ORIGIN,
    )
    assert merged.status_code == 200, merged.text
    body = merged.json()
    assert body["affected_site_count"] == 2
    assert body["already_tagged_count"] == 1
    assert body["target"]["id"] == target["id"]
    assert body["target"]["site_count"] == 3
    assert "LLM" in body["message"] and "大模型" in body["message"]

    assert sorted(_tag_names(client, only_source["id"])) == sorted(["其它", "大模型"])
    assert _tag_names(client, both["id"]) == ["大模型"]
    assert _tag_names(client, only_target["id"]) == ["大模型"]
    listed = client.get("/api/library/tags").json()["items"]
    names = {tag["name"]: tag["site_count"] for tag in listed}
    assert names == {"大模型": 3, "其它": 1}

    # 受影响的收藏版本号 +1（编辑表单的乐观锁能感知），未受影响的不动
    assert client.get(f"/api/library/sites/{only_source['id']}").json()["version"] == 2
    assert client.get(f"/api/library/sites/{both['id']}").json()["version"] == 2
    assert client.get(f"/api/library/sites/{only_target['id']}").json()["version"] == 1

    # 合并是用户对标签的明确决定：之后的自动分析不能把标签改回去
    with sqlite3.connect(database_path) as connection:
        rows = {
            site_id: (manual, llm)
            for site_id, manual, llm in connection.execute(
                "SELECT site_id, tags_are_manual, tags_are_llm FROM site_metadata_preferences"
            ).fetchall()
        }
        dangling = connection.execute(
            "SELECT COUNT(*) FROM site_tags WHERE tag_id = ?", (source["id"],)
        ).fetchone()[0]
    assert rows[only_source["id"]] == (1, 0)
    assert rows[both["id"]] == (1, 0)
    assert dangling == 0

    gone = client.get(
        f"/api/library/tags/{source['id']}/merge-preview",
        params={"target_id": target["id"]},
    )
    assert gone.status_code == 404


def test_merging_an_unused_tag_just_removes_it(client: TestClient) -> None:
    _register(client, "alice")
    source = _tag(client, "空标签")
    target = _tag(client, "目标")
    merged = client.post(
        f"/api/library/tags/{source['id']}/merge",
        json={"target_id": target["id"]},
        headers=ORIGIN,
    )
    assert merged.status_code == 200, merged.text
    assert merged.json()["affected_site_count"] == 0
    assert [tag["name"] for tag in client.get("/api/library/tags").json()["items"]] == ["目标"]


def test_merge_rejects_self_unknown_and_extra_fields(client: TestClient) -> None:
    _register(client, "alice")
    tag = _tag(client, "自己")
    same = client.post(
        f"/api/library/tags/{tag['id']}/merge",
        json={"target_id": tag["id"]},
        headers=ORIGIN,
    )
    assert same.status_code == 422
    assert same.json()["detail"]["code"] == "tag_merge_same_tag"
    same_preview = client.get(
        f"/api/library/tags/{tag['id']}/merge-preview", params={"target_id": tag["id"]}
    )
    assert same_preview.status_code == 422

    missing = client.post(
        f"/api/library/tags/{tag['id']}/merge",
        json={"target_id": "00000000-0000-0000-0000-000000000000"},
        headers=ORIGIN,
    )
    assert missing.status_code == 404

    extra = client.post(
        f"/api/library/tags/{tag['id']}/merge",
        json={"target_id": tag["id"], "delete_source": False},
        headers=ORIGIN,
    )
    assert extra.status_code == 422
    assert client.get(f"/api/library/tags/{tag['id']}/merge-preview").status_code == 422

    no_origin = client.post(f"/api/library/tags/{tag['id']}/merge", json={"target_id": tag["id"]})
    assert no_origin.status_code == 403
    assert [item["name"] for item in client.get("/api/library/tags").json()["items"]] == ["自己"]


def test_merge_is_account_scoped(client: TestClient) -> None:
    alice = _register(client, "alice")
    alice_tag = _tag(client, "alice-tag")
    alice_site = _site(client, "https://alice.example.com", [str(alice_tag["id"])])
    bob = _register(client, "bobby")
    _use(client, bob)
    bob_tag = _tag(client, "bob-tag")

    for source, target in ((alice_tag, bob_tag), (bob_tag, alice_tag)):
        preview = client.get(
            f"/api/library/tags/{source['id']}/merge-preview",
            params={"target_id": target["id"]},
        )
        assert preview.status_code == 404
        merged = client.post(
            f"/api/library/tags/{source['id']}/merge",
            json={"target_id": target["id"]},
            headers=ORIGIN,
        )
        assert merged.status_code == 404

    _use(client, alice)
    assert _tag_names(client, alice_site["id"]) == ["alice-tag"]
    _use(client, bob)
    assert [tag["name"] for tag in client.get("/api/library/tags").json()["items"]] == ["bob-tag"]


def test_merge_requires_a_session(client: TestClient) -> None:
    _register(client, "alice")
    source = _tag(client, "a")
    target = _tag(client, "b")
    client.cookies.clear()
    response = client.post(
        f"/api/library/tags/{source['id']}/merge",
        json={"target_id": target["id"]},
        headers=ORIGIN,
    )
    assert response.status_code == 401


# ── 分类图标白名单 ───────────────────────────────────────────────────────


def _category(client: TestClient, name: str, icon: str | None = None) -> dict[str, object]:
    payload: dict[str, object] = {"name": name}
    if icon is not None:
        payload["icon"] = icon
    response = client.post("/api/library/categories", json=payload, headers=ORIGIN)
    assert response.status_code == 201, response.text
    return response.json()


def test_category_icon_can_be_picked_kept_and_reset(client: TestClient) -> None:
    _register(client, "alice")
    category = _category(client, "阅读清单", icon="Star")
    assert category["icon"] == "Star"

    renamed = client.patch(
        f"/api/library/categories/{category['id']}",
        json={"name": "稍后读"},
        headers=ORIGIN,
    )
    assert renamed.json()["icon"] == "Star", "纯重命名不动手选的图标"

    picked = client.patch(
        f"/api/library/categories/{category['id']}",
        json={"name": "稍后读", "icon": " Coffee "},
        headers=ORIGIN,
    )
    assert picked.status_code == 200, picked.text
    assert picked.json()["icon"] == "Coffee"

    reset = client.patch(
        f"/api/library/categories/{category['id']}",
        json={"name": "阅读清单", "icon": ""},
        headers=ORIGIN,
    )
    assert reset.json()["icon"] == infer_category_icon("阅读清单")


def test_category_icon_outside_the_whitelist_is_rejected(client: TestClient) -> None:
    _register(client, "alice")
    for icon in ("Loader2", "<svg>", "star", "x" * 41):
        created = client.post(
            "/api/library/categories",
            json={"name": f"分类 {len(icon)} {icon[:3]}", "icon": icon},
            headers=ORIGIN,
        )
        assert created.status_code == 422, icon
    category = _category(client, "工具箱")
    updated = client.patch(
        f"/api/library/categories/{category['id']}",
        json={"name": "工具箱", "icon": "AlertCircle"},
        headers=ORIGIN,
    )
    assert updated.status_code == 422
    assert client.get("/api/library/categories").json()["items"][-1]["icon"] == category["icon"]


def test_inferred_icons_stay_inside_the_whitelist() -> None:
    assert set(SEMANTIC_ICONS.values()) | {"Folder"} <= CATEGORY_ICON_NAMES


def test_frontend_icon_picker_and_renderer_match_the_whitelist() -> None:
    picker = (REPO_ROOT / "apps/web/lib/category-icons.ts").read_text(encoding="utf-8")
    renderer = (REPO_ROOT / "apps/web/components/ui/dynamic-icon.tsx").read_text(encoding="utf-8")
    picker_names = set(re.findall(r'name: "([A-Za-z0-9]+)"', picker))
    map_body = renderer.split("ICON_MAP", 1)[1].split("};", 1)[0]
    renderer_names = set(re.findall(r"\b([A-Z][A-Za-z0-9]+)\b", map_body)) - {
        "Record",
        "React",
        "FC",
        "LucideProps",
    }
    assert picker_names == set(CATEGORY_ICON_NAMES)
    assert renderer_names == set(CATEGORY_ICON_NAMES)
