from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from webhub.config import Settings
from webhub.db.migrations import upgrade_database
from webhub.library import similarity as site_similarity
from webhub.main import create_app

COOKIE_NAME = "webhub_session"
ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture
def similarity_client(tmp_path: Path) -> Iterator[tuple[TestClient, Path]]:
    database_path = tmp_path / "main.sqlite3"
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        data_directory=tmp_path,
    )
    upgrade_database(settings.database_url)
    with TestClient(create_app(settings=settings)) as client:
        yield client, database_path


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


def _site(
    client: TestClient,
    name: str,
    url: str,
    *,
    tag_ids: list[str] | None = None,
    pinned: bool = False,
) -> dict[str, object]:
    response = client.post(
        "/api/library/sites",
        json={
            "name": name,
            "url": url,
            "tag_ids": tag_ids or [],
            "pinned": pinned,
        },
        headers=ORIGIN,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _start_scan(client: TestClient) -> dict[str, object]:
    response = client.post("/api/library/site-similarity-scans", headers=ORIGIN)
    assert response.status_code == 201, response.text
    return response.json()


def _groups(client: TestClient, run_id: object) -> list[dict[str, object]]:
    response = client.get(
        f"/api/library/site-similarity-scans/{run_id}/groups",
        params={"kind": "all", "limit": 12},
    )
    assert response.status_code == 200, response.text
    return response.json()["items"]


def _group_page(
    client: TestClient,
    run_id: object,
    *,
    kind: str = "all",
    limit: int = 12,
    page: int | None = 1,
    cursor: str | None = None,
) -> dict[str, object]:
    params: dict[str, object] = {"kind": kind, "limit": limit}
    if page is not None:
        params["page"] = page
    if cursor is not None:
        params["cursor"] = cursor
    response = client.get(
        f"/api/library/site-similarity-scans/{run_id}/groups",
        params=params,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _duplicate_pair(
    client: TestClient,
    index: int,
) -> tuple[dict[str, object], dict[str, object]]:
    return (
        _site(client, f"Duplicate {index}", f"https://duplicate-{index}.example/resource"),
        _site(
            client,
            f"Tracked duplicate {index}",
            f"http://www.duplicate-{index}.example/resource/?utm_source=test",
        ),
    )


def _same_site_pair(
    client: TestClient,
    index: int,
) -> tuple[dict[str, object], dict[str, object]]:
    return (
        _site(client, f"Guide {index}", f"https://same-{index}.example/guide"),
        _site(client, f"Pricing {index}", f"https://same-{index}.example/pricing"),
    )


def _wrong_recommendation(group: dict[str, object]) -> str:
    recommended_site_id = group["recommended_site_id"]
    return next(
        str(member["id"])
        for member in group["members"]  # type: ignore[union-attr]
        if member["id"] != recommended_site_id
    )


def test_scan_partitions_variants_and_apply_preserves_user_relationships(
    similarity_client: tuple[TestClient, Path],
) -> None:
    client, database_path = similarity_client
    _register(client, "alice")
    tag = client.post("/api/library/tags", json={"name": "参考"}, headers=ORIGIN).json()
    keeper = _site(client, "Docs", "https://example.com/docs")
    loser = _site(
        client,
        "Tracked docs",
        "http://www.example.com/docs/?utm_source=newsletter#intro",
        tag_ids=[tag["id"]],
        pinned=True,
    )
    first_page = _site(client, "Guide", "https://example.com/guide")
    second_page = _site(client, "Pricing", "https://example.com/pricing")
    _site(client, "Shared", "https://github.com/example/project")
    _site(client, "Sensitive", "https://example.com/private?token=secret")

    space = client.post("/api/spaces", json={"name": "Reading"}, headers=ORIGIN).json()
    added = client.post(
        f"/api/spaces/{space['id']}/members",
        json={"expected_version": space["version"], "site_id": loser["id"]},
        headers=ORIGIN,
    )
    assert added.status_code == 201, added.text

    scan = _start_scan(client)
    assert scan["source_site_count"] == 6
    assert scan["duplicate_group_count"] == 1
    assert scan["same_site_group_count"] == 1
    assert scan["candidate_site_count"] == 4
    groups = _groups(client, scan["id"])
    duplicate = next(group for group in groups if group["kind"] == "duplicate")
    same_site = next(group for group in groups if group["kind"] == "same_site")
    assert {member["id"] for member in duplicate["members"]} == {
        keeper["id"],
        loser["id"],
    }
    assert {member["id"] for member in same_site["members"]} == {
        first_page["id"],
        second_page["id"],
    }
    assert duplicate["recommended_site_id"] == keeper["id"]
    assert same_site["keep_site_ids"] == []

    decision = client.put(
        f"/api/library/site-similarity-scans/{scan['id']}/groups/{duplicate['id']}/decision",
        json={
            "keep_site_ids": [keeper["id"]],
            "expected_version": scan["decision_version"],
        },
        headers=ORIGIN,
    )
    assert decision.status_code == 200, decision.text
    assert decision.json()["selected_delete_count"] == 1

    applied = client.post(
        f"/api/library/site-similarity-scans/{scan['id']}/apply",
        json={"expected_version": decision.json()["decision_version"]},
        headers=ORIGIN,
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["deleted_site_ids"] == [loser["id"]]
    assert client.get(f"/api/library/sites/{loser['id']}").status_code == 404

    kept = client.get(f"/api/library/sites/{keeper['id']}").json()
    assert kept["name"] == "Docs"
    assert kept["identity_url"] == "https://example.com/docs"
    assert kept["pinned"] is True
    assert [item["name"] for item in kept["tags"]] == ["参考"]
    assert client.get(f"/api/library/sites/{first_page['id']}").status_code == 200
    assert client.get(f"/api/library/sites/{second_page['id']}").status_code == 200

    space_after = client.get(f"/api/spaces/{space['id']}").json()
    assert [member["site"]["id"] for member in space_after["members"]] == [keeper["id"]]
    with sqlite3.connect(database_path) as connection:
        preference = connection.execute(
            "SELECT tags_are_manual, tags_are_llm FROM site_metadata_preferences "
            "WHERE site_id = ?",
            (keeper["id"],),
        ).fetchone()
    assert preference == (1, 0)

    replayed = client.post(
        f"/api/library/site-similarity-scans/{scan['id']}/apply",
        json={"expected_version": decision.json()["decision_version"]},
        headers=ORIGIN,
    )
    assert replayed.status_code == 200
    assert replayed.json() == applied.json()


def test_stale_scan_and_decision_version_conflicts_delete_nothing(
    similarity_client: tuple[TestClient, Path],
) -> None:
    client, _ = similarity_client
    alice_token = _register(client, "alice")
    first = _site(client, "First", "https://stale.example/path")
    second = _site(client, "Second", "http://www.stale.example/path/")
    unrelated = _site(client, "Other", "https://other.example/")
    scan = _start_scan(client)
    group = _groups(client, scan["id"])[0]

    accepted = client.put(
        f"/api/library/site-similarity-scans/{scan['id']}/groups/{group['id']}/decision",
        json={"keep_site_ids": [first["id"]], "expected_version": scan["decision_version"]},
        headers=ORIGIN,
    )
    assert accepted.status_code == 200
    stale_decision = client.put(
        f"/api/library/site-similarity-scans/{scan['id']}/groups/{group['id']}/decision",
        json={"keep_site_ids": [second["id"]], "expected_version": scan["decision_version"]},
        headers=ORIGIN,
    )
    assert stale_decision.status_code == 409
    assert stale_decision.json()["detail"]["code"] == "site_similarity_version_conflict"

    updated = client.patch(
        f"/api/library/sites/{unrelated['id']}",
        json={"expected_version": unrelated["version"], "pinned": True},
        headers=ORIGIN,
    )
    assert updated.status_code == 200
    rejected = client.post(
        f"/api/library/site-similarity-scans/{scan['id']}/apply",
        json={"expected_version": accepted.json()["decision_version"]},
        headers=ORIGIN,
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "site_similarity_scan_stale"
    assert client.get(f"/api/library/sites/{first['id']}").status_code == 200
    assert client.get(f"/api/library/sites/{second['id']}").status_code == 200

    client.cookies.clear()
    _register(client, "bob")
    assert client.get(
        f"/api/library/site-similarity-scans/{scan['id']}/groups"
    ).status_code == 404
    _use_token(client, alice_token)
    assert client.get("/api/library/site-similarity-scans/active").json()["id"] == scan["id"]


def test_apply_and_decision_update_use_one_atomic_version_reservation(
    similarity_client: tuple[TestClient, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = similarity_client
    _register(client, "alice")
    first = _site(client, "First", "https://apply-race.example/resource")
    second = _site(client, "Second", "http://www.apply-race.example/resource/")
    scan = _start_scan(client)
    group = _groups(client, scan["id"])[0]
    initial = client.put(
        f"/api/library/site-similarity-scans/{scan['id']}/groups/{group['id']}/decision",
        json={
            "keep_site_ids": [first["id"]],
            "expected_version": scan["decision_version"],
        },
        headers=ORIGIN,
    )
    assert initial.status_code == 200, initial.text

    apply_reserved = threading.Event()
    release_apply = threading.Event()
    original_fingerprint = site_similarity.library_fingerprint

    def blocked_fingerprint(sites: object) -> str:
        apply_reserved.set()
        if not release_apply.wait(timeout=5):
            raise AssertionError("timed out waiting to release apply")
        return original_fingerprint(sites)  # type: ignore[arg-type]

    monkeypatch.setattr(site_similarity, "library_fingerprint", blocked_fingerprint)
    apply_url = f"/api/library/site-similarity-scans/{scan['id']}/apply"
    decision_url = (
        f"/api/library/site-similarity-scans/{scan['id']}/groups/{group['id']}/decision"
    )
    expected_version = initial.json()["decision_version"]

    with ThreadPoolExecutor(max_workers=2) as executor:
        apply_future = executor.submit(
            client.post,
            apply_url,
            json={"expected_version": expected_version},
            headers=ORIGIN,
        )
        assert apply_reserved.wait(timeout=5)
        decision_future = executor.submit(
            client.put,
            decision_url,
            json={
                "keep_site_ids": [second["id"]],
                "expected_version": expected_version,
            },
            headers=ORIGIN,
        )
        try:
            with pytest.raises(TimeoutError):
                decision_future.result(timeout=0.3)
        finally:
            release_apply.set()
        applied = apply_future.result(timeout=5)
        raced_decision = decision_future.result(timeout=5)

    assert applied.status_code == 200, applied.text
    assert applied.json()["deleted_site_ids"] == [second["id"]]
    assert raced_decision.status_code == 409, raced_decision.text
    assert raced_decision.json()["detail"]["code"] == "site_similarity_version_conflict"
    assert client.get(f"/api/library/sites/{first['id']}").status_code == 200
    assert client.get(f"/api/library/sites/{second['id']}").status_code == 404


def test_similarity_writes_require_authentication_and_trusted_origin(
    similarity_client: tuple[TestClient, Path],
) -> None:
    client, _ = similarity_client
    assert client.get("/api/library/site-similarity-scans/active").status_code == 401
    _register(client, "alice")
    assert client.get("/api/library/site-similarity-scans/active").json() is None
    assert client.post("/api/library/site-similarity-scans").status_code == 403
    assert client.post(
        "/api/library/site-similarity-scans",
        headers={"Origin": "http://attacker.invalid"},
    ).status_code == 403


def test_group_pages_report_filter_totals_and_support_direct_navigation(
    similarity_client: tuple[TestClient, Path],
) -> None:
    client, _ = similarity_client
    _register(client, "alice")
    for index in range(3):
        _duplicate_pair(client, index)
    for index in range(2):
        _same_site_pair(client, index)
    scan = _start_scan(client)

    all_first = _group_page(client, scan["id"], kind="all", limit=2, page=1)
    all_second = _group_page(client, scan["id"], kind="all", limit=2, page=2)
    duplicate_second = _group_page(
        client,
        scan["id"],
        kind="duplicate",
        limit=2,
        page=2,
    )
    same_site = _group_page(client, scan["id"], kind="same_site", limit=2, page=1)

    assert (all_first["page"], all_first["page_size"]) == (1, 2)
    assert (all_first["total_count"], all_first["total_pages"]) == (5, 3)
    assert (all_second["page"], all_second["total_count"], all_second["total_pages"]) == (
        2,
        5,
        3,
    )
    assert {group["id"] for group in all_first["items"]}.isdisjoint(  # type: ignore[union-attr]
        {group["id"] for group in all_second["items"]}  # type: ignore[union-attr]
    )
    assert (duplicate_second["total_count"], duplicate_second["total_pages"]) == (3, 2)
    assert len(duplicate_second["items"]) == 1  # type: ignore[arg-type]
    assert all(
        group["kind"] == "duplicate"
        for group in duplicate_second["items"]  # type: ignore[union-attr]
    )
    assert (same_site["total_count"], same_site["total_pages"]) == (2, 1)
    assert all(
        group["kind"] == "same_site"
        for group in same_site["items"]  # type: ignore[union-attr]
    )

    out_of_range = client.get(
        f"/api/library/site-similarity-scans/{scan['id']}/groups",
        params={"kind": "same_site", "limit": 2, "page": 2},
    )
    assert out_of_range.status_code == 422
    assert out_of_range.json()["detail"]["code"] == "invalid_similarity_page"

    cursor_and_page = client.get(
        f"/api/library/site-similarity-scans/{scan['id']}/groups",
        params={
            "kind": "all",
            "limit": 2,
            "cursor": all_first["next_cursor"],
            "page": 2,
        },
    )
    assert cursor_and_page.status_code == 422
    assert cursor_and_page.json()["detail"]["code"] == "invalid_similarity_pagination"


def test_empty_group_page_and_bulk_scope_advance_one_version(
    similarity_client: tuple[TestClient, Path],
) -> None:
    client, _ = similarity_client
    _register(client, "alice")
    _duplicate_pair(client, 0)
    scan = _start_scan(client)

    empty_page = _group_page(client, scan["id"], kind="same_site", limit=2, page=1)
    assert empty_page["items"] == []
    assert (empty_page["total_count"], empty_page["total_pages"]) == (0, 0)

    selected = client.put(
        f"/api/library/site-similarity-scans/{scan['id']}/decisions/recommended",
        json={"kind": "same_site", "expected_version": scan["decision_version"]},
        headers=ORIGIN,
    )
    assert selected.status_code == 200, selected.text
    assert selected.json() == {
        "kind": "same_site",
        "matched_group_count": 0,
        "updated_group_count": 0,
        "decision_version": scan["decision_version"] + 1,
        "selected_group_count": 0,
        "selected_delete_count": 0,
    }
    after = _group_page(client, scan["id"], kind="duplicate", limit=2, page=1)
    assert after["decision_version"] == scan["decision_version"] + 1
    assert after["items"][0]["keep_site_ids"] == []  # type: ignore[index]


def test_bulk_recommendations_are_partition_scoped_and_cover_every_page(
    similarity_client: tuple[TestClient, Path],
) -> None:
    client, _ = similarity_client
    _register(client, "alice")
    for index in range(5):
        _duplicate_pair(client, index)
    _site(
        client,
        "Extra duplicate 0",
        "https://duplicate-0.example/resource?utm_campaign=bulk",
    )
    for index in range(2):
        _same_site_pair(client, index)
    scan = _start_scan(client)
    groups = _groups(client, scan["id"])
    duplicates = [group for group in groups if group["kind"] == "duplicate"]
    same_sites = [group for group in groups if group["kind"] == "same_site"]

    multi_duplicate = next(group for group in duplicates if group["member_count"] == 3)
    wrong_duplicate = _wrong_recommendation(multi_duplicate)
    duplicate_decision = client.put(
        f"/api/library/site-similarity-scans/{scan['id']}/groups/"
        f"{multi_duplicate['id']}/decision",
        json={
            "keep_site_ids": [multi_duplicate["recommended_site_id"], wrong_duplicate],
            "expected_version": scan["decision_version"],
        },
        headers=ORIGIN,
    )
    assert duplicate_decision.status_code == 200, duplicate_decision.text
    assert set(duplicate_decision.json()["keep_site_ids"]) == {
        multi_duplicate["recommended_site_id"],
        wrong_duplicate,
    }
    wrong_same_site = _wrong_recommendation(same_sites[0])
    same_site_decision = client.put(
        f"/api/library/site-similarity-scans/{scan['id']}/groups/"
        f"{same_sites[0]['id']}/decision",
        json={
            "keep_site_ids": [wrong_same_site],
            "expected_version": duplicate_decision.json()["decision_version"],
        },
        headers=ORIGIN,
    )
    assert same_site_decision.status_code == 200, same_site_decision.text

    selected = client.put(
        f"/api/library/site-similarity-scans/{scan['id']}/decisions/recommended",
        json={
            "kind": "duplicate",
            "expected_version": same_site_decision.json()["decision_version"],
        },
        headers=ORIGIN,
    )
    assert selected.status_code == 200, selected.text
    body = selected.json()
    assert body["matched_group_count"] == 5
    assert body["updated_group_count"] == 5
    assert body["decision_version"] == same_site_decision.json()["decision_version"] + 1
    assert body["selected_group_count"] == 6
    assert body["selected_delete_count"] == 7

    first_duplicate_page = _group_page(
        client,
        scan["id"],
        kind="duplicate",
        limit=2,
        page=1,
    )
    for page_number in range(1, int(first_duplicate_page["total_pages"]) + 1):
        page = _group_page(
            client,
            scan["id"],
            kind="duplicate",
            limit=2,
            page=page_number,
        )
        assert all(
            group["keep_site_ids"] == [group["recommended_site_id"]]
            for group in page["items"]  # type: ignore[union-attr]
        )

    same_site_after = _group_page(
        client,
        scan["id"],
        kind="same_site",
        limit=2,
        page=1,
    )
    choices = {group["id"]: group["keep_site_ids"] for group in same_site_after["items"]}  # type: ignore[union-attr]
    assert choices[same_sites[0]["id"]] == [wrong_same_site]
    assert choices[same_sites[1]["id"]] == []

    repeated = client.put(
        f"/api/library/site-similarity-scans/{scan['id']}/decisions/recommended",
        json={"kind": "duplicate", "expected_version": body["decision_version"]},
        headers=ORIGIN,
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["updated_group_count"] == 0
    assert repeated.json()["decision_version"] == body["decision_version"] + 1


def test_stale_bulk_version_has_no_partial_writes(
    similarity_client: tuple[TestClient, Path],
) -> None:
    client, _ = similarity_client
    _register(client, "alice")
    _duplicate_pair(client, 0)
    _same_site_pair(client, 0)
    scan = _start_scan(client)
    groups = _groups(client, scan["id"])
    duplicate = next(group for group in groups if group["kind"] == "duplicate")

    accepted = client.put(
        f"/api/library/site-similarity-scans/{scan['id']}/groups/{duplicate['id']}/decision",
        json={
            "keep_site_ids": [duplicate["recommended_site_id"]],
            "expected_version": scan["decision_version"],
        },
        headers=ORIGIN,
    )
    assert accepted.status_code == 200, accepted.text
    stale = client.put(
        f"/api/library/site-similarity-scans/{scan['id']}/decisions/recommended",
        json={"kind": "same_site", "expected_version": scan["decision_version"]},
        headers=ORIGIN,
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "site_similarity_version_conflict"

    same_site_after = _group_page(client, scan["id"], kind="same_site", page=1)
    assert same_site_after["decision_version"] == accepted.json()["decision_version"]
    assert all(
        group["keep_site_ids"] == []
        for group in same_site_after["items"]  # type: ignore[union-attr]
    )


def test_bulk_recommendations_validate_scope_auth_origin_and_account(
    similarity_client: tuple[TestClient, Path],
) -> None:
    client, _ = similarity_client
    alice_token = _register(client, "alice")
    _duplicate_pair(client, 0)
    scan = _start_scan(client)
    url = f"/api/library/site-similarity-scans/{scan['id']}/decisions/recommended"
    payload = {"kind": "duplicate", "expected_version": scan["decision_version"]}

    missing_kind = client.put(
        url,
        json={"expected_version": scan["decision_version"]},
        headers=ORIGIN,
    )
    assert missing_kind.status_code == 422
    invalid_kind = client.put(
        url,
        json={"kind": "unknown", "expected_version": scan["decision_version"]},
        headers=ORIGIN,
    )
    assert invalid_kind.status_code == 422

    client.cookies.clear()
    assert client.put(url, json=payload, headers=ORIGIN).status_code == 401
    _use_token(client, alice_token)
    assert client.put(url, json=payload).status_code == 403
    assert client.put(
        url,
        json=payload,
        headers={"Origin": "http://attacker.invalid"},
    ).status_code == 403

    client.cookies.clear()
    _register(client, "bob")
    assert client.put(url, json=payload, headers=ORIGIN).status_code == 404

    _use_token(client, alice_token)
    unchanged = _group_page(client, scan["id"], kind="duplicate", page=1)
    assert unchanged["decision_version"] == scan["decision_version"]
    assert unchanged["items"][0]["keep_site_ids"] == []  # type: ignore[index]


def test_multi_select_keeps_selected_members_and_only_merges_deleted_relationships(
    similarity_client: tuple[TestClient, Path],
) -> None:
    client, database_path = similarity_client
    _register(client, "alice")
    tag = client.post("/api/library/tags", json={"name": "待转移"}, headers=ORIGIN).json()
    recommended = _site(
        client,
        "Recommended",
        "https://multi.example/resource",
        tag_ids=[tag["id"]],
        pinned=True,
    )
    first_survivor = _site(client, "First survivor", "http://www.multi.example/resource/")
    second_survivor = _site(
        client,
        "Second survivor",
        "https://multi.example/resource/?utm_source=test",
    )
    space = client.post("/api/spaces", json={"name": "Review"}, headers=ORIGIN).json()
    added_survivor = client.post(
        f"/api/spaces/{space['id']}/members",
        json={"expected_version": space["version"], "site_id": second_survivor["id"]},
        headers=ORIGIN,
    )
    assert added_survivor.status_code == 201, added_survivor.text
    space_after_survivor = client.get(f"/api/spaces/{space['id']}").json()
    added_loser = client.post(
        f"/api/spaces/{space['id']}/members",
        json={
            "expected_version": space_after_survivor["version"],
            "site_id": recommended["id"],
        },
        headers=ORIGIN,
    )
    assert added_loser.status_code == 201, added_loser.text

    scan = _start_scan(client)
    group = _groups(client, scan["id"])[0]
    assert group["member_count"] == 3
    assert group["recommended_site_id"] == recommended["id"]
    decision = client.put(
        f"/api/library/site-similarity-scans/{scan['id']}/groups/{group['id']}/decision",
        json={
            "keep_site_ids": [second_survivor["id"], first_survivor["id"]],
            "expected_version": scan["decision_version"],
        },
        headers=ORIGIN,
    )
    assert decision.status_code == 200, decision.text
    assert decision.json()["keep_site_ids"] == [first_survivor["id"], second_survivor["id"]]
    assert decision.json()["selected_group_count"] == 1
    assert decision.json()["selected_delete_count"] == 1
    with sqlite3.connect(database_path) as connection:
        primary_keep_id = connection.execute(
            "SELECT keep_site_id FROM site_similarity_decisions WHERE group_id = ?",
            (group["id"],),
        ).fetchone()
        selected_ids = {
            row[0]
            for row in connection.execute(
                "SELECT site_id FROM site_similarity_decision_members WHERE group_id = ?",
                (group["id"],),
            )
        }
    assert primary_keep_id == (first_survivor["id"],)
    assert selected_ids == {first_survivor["id"], second_survivor["id"]}

    applied = client.post(
        f"/api/library/site-similarity-scans/{scan['id']}/apply",
        json={"expected_version": decision.json()["decision_version"]},
        headers=ORIGIN,
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["merged_group_count"] == 1
    assert applied.json()["deleted_site_ids"] == [recommended["id"]]
    assert set(applied.json()["kept_site_ids"]) == {
        first_survivor["id"],
        second_survivor["id"],
    }
    assert client.get(f"/api/library/sites/{recommended['id']}").status_code == 404
    assert client.get(f"/api/library/sites/{second_survivor['id']}").status_code == 200
    primary_after = client.get(f"/api/library/sites/{first_survivor['id']}").json()
    assert primary_after["pinned"] is True
    assert [item["name"] for item in primary_after["tags"]] == ["待转移"]
    space_after = client.get(f"/api/spaces/{space['id']}").json()
    assert {member["site"]["id"] for member in space_after["members"]} == {
        first_survivor["id"],
        second_survivor["id"],
    }


def test_multi_select_validation_and_all_keep_normalization(
    similarity_client: tuple[TestClient, Path],
) -> None:
    client, database_path = similarity_client
    _register(client, "alice")
    first = _site(client, "First", "https://all-keep.example/resource")
    second = _site(client, "Second", "http://www.all-keep.example/resource/")
    third = _site(
        client,
        "Third",
        "https://all-keep.example/resource/?utm_source=test",
    )
    unrelated = _site(client, "Unrelated", "https://unrelated.example/")
    scan = _start_scan(client)
    group = _groups(client, scan["id"])[0]
    decision_url = (
        f"/api/library/site-similarity-scans/{scan['id']}/groups/{group['id']}/decision"
    )

    duplicate = client.put(
        decision_url,
        json={
            "keep_site_ids": [first["id"], first["id"]],
            "expected_version": scan["decision_version"],
        },
        headers=ORIGIN,
    )
    assert duplicate.status_code == 422
    outside_group = client.put(
        decision_url,
        json={
            "keep_site_ids": [unrelated["id"]],
            "expected_version": scan["decision_version"],
        },
        headers=ORIGIN,
    )
    assert outside_group.status_code == 422
    assert outside_group.json()["detail"]["code"] == "invalid_similarity_keep_sites"
    unchanged = _group_page(client, scan["id"], kind="duplicate", page=1)
    assert unchanged["decision_version"] == scan["decision_version"]
    assert unchanged["items"][0]["keep_site_ids"] == []  # type: ignore[index]

    all_kept = client.put(
        decision_url,
        json={
            "keep_site_ids": [third["id"], first["id"], second["id"]],
            "expected_version": scan["decision_version"],
        },
        headers=ORIGIN,
    )
    assert all_kept.status_code == 200, all_kept.text
    assert all_kept.json()["keep_site_ids"] == []
    assert all_kept.json()["selected_group_count"] == 0
    assert all_kept.json()["selected_delete_count"] == 0
    with sqlite3.connect(database_path) as connection:
        decision_row = connection.execute(
            "SELECT keep_site_id FROM site_similarity_decisions WHERE group_id = ?",
            (group["id"],),
        ).fetchone()
        selected_count = connection.execute(
            "SELECT COUNT(*) FROM site_similarity_decision_members WHERE group_id = ?",
            (group["id"],),
        ).fetchone()
    assert decision_row == (None,)
    assert selected_count == (0,)
    for site in (first, second, third, unrelated):
        assert client.get(f"/api/library/sites/{site['id']}").status_code == 200
