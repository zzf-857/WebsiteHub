from __future__ import annotations

import asyncio
import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from webhub.db.database import Database
from webhub.portability import importer

from .conftest import ORIGIN, Environment


def preview(env: Environment, backup: dict[str, Any]) -> dict[str, Any]:
    return env.post("/api/portability/imports", backup)


def apply(env: Environment, result: dict[str, Any]) -> dict[str, Any]:
    return env.post(
        f"/api/portability/imports/{result['id']}/apply",
        {"expected_sha256": result["payload_sha256"]},
    )


def test_preview_apply_idempotent_and_checksum(env: Environment, backup: dict[str, Any]) -> None:
    result = preview(env, backup)
    assert env.query("SELECT COUNT(*) FROM sites")[0][0] == 0
    assert result["counts"]["sites"] == {"create": 1, "merge": 0, "skip_invalid": 0}
    assert result["counts"]["conversations"]["skipped"] == 1
    assert len(result["samples"]["categories_create"]) == 1
    assert env.client.get(f"/api/portability/imports/{result['id']}").json() == result
    bad = env.client.post(
        f"/api/portability/imports/{result['id']}/apply",
        json={"expected_sha256": "0" * 64},
        headers=ORIGIN,
    )
    assert bad.status_code == 409 and bad.json()["detail"]["code"] == "checksum_mismatch"
    first = apply(env, result)
    assert first["created"] == {"categories": 1, "tags": 1, "sites": 1, "spaces": 1, "feeds": 1}
    assert apply(env, result) == first
    assert env.query("SELECT COUNT(*) FROM sites")[0][0] == 1
    assert env.query("SELECT source,analysis_status FROM sites") == [("backup", "complete")]
    assert env.query("SELECT is_default FROM categories WHERE name='AI 与 Agent'") == [(0,)]
    assert not list((env.directory / "portability-imports").rglob("*.json"))
    assert env.query("SELECT COUNT(*) FROM conversations")[0][0] == 0


def test_merge_fields_signals_names_versions_and_space_order(
    env: Environment, backup: dict[str, Any]
) -> None:
    apply(env, preview(env, backup))
    env.execute(
        "UPDATE sites SET summary='保留摘要', description='', open_count=3, "
        "last_opened_at='2026-10-20', seen_at='2026-10-15', starred_at='2026-10-01', "
        "meta_json=?",
        json.dumps({"github": {"stars": 99}, "kept": 1}),
    )
    before = env.query("SELECT name,category_id,pinned,version,updated_at FROM sites")[0]
    old_space = env.query("SELECT version,updated_at FROM spaces")[0]
    backup["sites"][0].update(
        name="不能改名",
        summary="新摘要",
        pinned=True,
        favicon_url="https://example.org/icon.png",
        preview_url="https://example.org/p.png",
        meta={"github": {"stars": 1, "language": "Python"}, "new": 2},
    )
    backup["tags"].append({"key": "t2", "name": "新增标签"})
    backup["sites"][0]["tag_keys"].append("t2")
    another = copy.deepcopy(backup["sites"][0])
    another.update(
        key="s2",
        url="http://localhost:9000/",
        name="本机收藏",
        summary="",
        description="",
        kind="invalid",
        kind_source="invalid",
    )
    backup["sites"].append(another)
    backup["spaces"][0]["site_keys"] = ["s2", "s1", "s2"]
    response = apply(env, preview(env, backup))
    assert response["created"]["sites"] == 1 and response["merged"]["sites"] == 1
    assert (
        env.query(
            "SELECT name,category_id,pinned,version,updated_at FROM sites "
            "WHERE original_url='https://example.org/tool'"
        )[0]
        == before
    )
    row = env.query(
        "SELECT summary,description,open_count,last_opened_at,seen_at,starred_at,"
        "meta_json FROM sites WHERE original_url='https://example.org/tool'"
    )[0]
    assert row[:3] == ("保留摘要", "收藏详情", 8)
    assert row[3].startswith("2026-10-23") and row[4].startswith("2026-10-10")
    assert row[5].startswith("2026-10-01")
    assert json.loads(row[6]) == {
        "github": {"stars": 99, "language": "Python"},
        "kept": 1,
        "new": 2,
    }
    assert (
        env.query(
            "SELECT COUNT(*) FROM site_tags WHERE site_id=(SELECT id FROM sites "
            "WHERE original_url='https://example.org/tool')"
        )[0][0]
        == 2
    )
    assert env.query(
        "SELECT kind,kind_source,analysis_status FROM sites "
        "WHERE original_url='http://localhost:9000/'"
    ) == [("website", "auto", "not_analyzed")]
    assert env.query(
        "SELECT s.original_url FROM space_members m JOIN sites s ON s.id=m.site_id "
        "ORDER BY m.position"
    ) == [("https://example.org/tool",), ("http://localhost:9000/",)]
    changed_space = env.query("SELECT version,updated_at FROM spaces")[0]
    assert changed_space[0] == old_space[0] + 1 and changed_space[1] != old_space[1]
    apply(env, preview(env, backup))
    assert env.query("SELECT version,updated_at FROM spaces")[0] == changed_space


def test_identity_recomputed_invalid_skipped_and_normalized_names_reused(
    env: Environment, backup: dict[str, Any]
) -> None:
    backup["sites"][0]["identity_url"] = "https://forged.example/"
    backup["sites"][0]["url"] = "HTTPS://EXAMPLE.ORG:443/tool"
    invalid = copy.deepcopy(backup["sites"][0])
    invalid.update(key="invalid", url="javascript:alert(1)")
    backup["sites"].append(invalid)
    backup["tags"].append({"key": "same", "name": "ＡＧＥＮＴ"})
    backup["categories"][0]["icon"] = "InvalidIcon"
    result = preview(env, backup)
    assert result["counts"]["sites"]["skip_invalid"] == 1
    assert result["counts"]["tags"] == {"create": 1, "reuse": 1}
    assert result["warnings"][0]["code"] == "invalid_url"
    apply(env, result)
    assert env.query("SELECT identity_url FROM sites") == [("https://example.org/tool",)]
    assert env.query("SELECT icon FROM categories WHERE name='AI 与 Agent'") == [("Bot",)]


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("missing", "invalid_format"),
        ("extra", "invalid_format"),
        ("version", "unsupported_version"),
        ("reference", "invalid_format"),
        ("length", "limits_exceeded"),
        ("tags_limit", "limits_exceeded"),
        ("sites_limit", "limits_exceeded"),
        ("duplicate", "invalid_format"),
        ("bad_feed", "invalid_format"),
        ("wrong_type", "invalid_format"),
    ],
)
def test_invalid_backup_has_no_writes(
    env: Environment, backup: dict[str, Any], case: str, code: str
) -> None:
    if case == "missing":
        del backup["sites"][0]["name"]
    elif case == "extra":
        backup["sites"][0]["provider_secret"] = "not-allowed"
    elif case == "version":
        backup["version"] = 2
    elif case == "reference":
        backup["spaces"][0]["site_keys"] = ["absent"]
    elif case == "length":
        backup["tags"][0]["name"] = "x" * 41
    elif case == "tags_limit":
        backup["tags"] *= 20_001
    elif case == "sites_limit":
        minimal = {
            **backup["sites"][0],
            "key": "s",
            "url": "x",
            "name": "x",
            "summary": "",
            "description": "",
            "kind": "",
            "kind_source": "",
            "category_key": "c",
            "tag_keys": [],
            "meta": {},
            "created_at": "2026-01-01",
            "last_opened_at": None,
            "seen_at": None,
            "starred_at": None,
            "open_count": 0,
            "resurface_muted": False,
        }
        backup["sites"] = [minimal] * 200_001
        encoded = json.dumps(backup, separators=(",", ":")).encode()
        assert len(encoded) < 64 * 1024 * 1024
        response = env.client.post(
            "/api/portability/imports",
            content=encoded,
            headers={**ORIGIN, "Content-Type": "application/json"},
        )
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "limits_exceeded"
        assert env.query("SELECT COUNT(*) FROM portability_imports")[0][0] == 0
        return
    elif case == "duplicate":
        backup["tags"] *= 2
    elif case == "bad_feed":
        backup["feeds"][0]["url"] = "javascript:bad"
    else:
        backup["sites"][0]["pinned"] = "true"
    response = env.client.post("/api/portability/imports", json=backup, headers=ORIGIN)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == code
    assert env.query("SELECT COUNT(*) FROM portability_imports")[0][0] == 0
    assert env.query("SELECT COUNT(*) FROM sites")[0][0] == 0
    assert not list((env.directory / "portability-imports").rglob("*.json"))


def test_expiry_account_isolation_auth_and_origin(env: Environment, backup: dict[str, Any]) -> None:
    cookie_a = env.client.cookies.get("webhub_session")
    result = preview(env, backup)
    path = Path(env.query("SELECT payload_path FROM portability_imports")[0][0])
    env.register("beta")
    assert env.client.get(f"/api/portability/imports/{result['id']}").status_code == 404
    assert (
        env.client.post(
            f"/api/portability/imports/{result['id']}/apply",
            json={"expected_sha256": result["payload_sha256"]},
            headers=ORIGIN,
        ).status_code
        == 404
    )
    assert env.client.post("/api/portability/imports", json=backup).status_code == 403
    assert (
        env.client.post(
            f"/api/portability/imports/{result['id']}/apply",
            json={"expected_sha256": result["payload_sha256"]},
        ).status_code
        == 403
    )
    env.client.cookies.clear()
    for path_url in ("/api/portability/export", f"/api/portability/imports/{result['id']}"):
        assert env.client.get(path_url).status_code == 401
    assert (
        env.client.post("/api/portability/imports", json=backup, headers=ORIGIN).status_code == 401
    )
    assert (
        env.client.post(
            f"/api/portability/imports/{result['id']}/apply",
            json={"expected_sha256": result["payload_sha256"]},
            headers=ORIGIN,
        ).status_code
        == 401
    )
    env.client.cookies.set("webhub_session", cookie_a)
    env.execute(
        "UPDATE portability_imports SET expires_at=?",
        (datetime.now(UTC) - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S.%f"),
    )
    response = env.client.post(
        f"/api/portability/imports/{result['id']}/apply",
        json={"expected_sha256": result["payload_sha256"]},
        headers=ORIGIN,
    )
    assert response.status_code == 409 and response.json()["detail"]["code"] == "preview_expired"
    assert not path.exists()
    assert env.query("SELECT status FROM portability_imports") == [("expired",)]


def test_partial_failure_rolls_back_and_keeps_preview(
    env: Environment, backup: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    result = preview(env, backup)

    async def fail(*args: object, **kwargs: object) -> None:
        raise IntegrityError("injected", {}, RuntimeError("private-upstream-detail"))

    original = importer.restore_feed
    monkeypatch.setattr(importer, "restore_feed", fail)
    response = env.client.post(
        f"/api/portability/imports/{result['id']}/apply",
        json={"expected_sha256": result["payload_sha256"]},
        headers=ORIGIN,
    )
    assert response.status_code == 409
    assert "private-upstream-detail" not in response.text
    for table in ("sites", "tags", "spaces", "space_members", "feeds"):
        assert env.query(f"SELECT COUNT(*) FROM {table}")[0][0] == 0
    assert env.query("SELECT COUNT(*) FROM categories WHERE is_default=0")[0][0] == 0
    assert env.query("SELECT status,result_json FROM portability_imports") == [("previewed", None)]
    assert Path(env.query("SELECT payload_path FROM portability_imports")[0][0]).exists()
    monkeypatch.setattr(importer, "restore_feed", original)
    assert apply(env, result)["created"]["sites"] == 1


def test_concurrent_apply_replays_one_receipt(
    env: Environment, backup: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    result = preview(env, backup)
    entered, release, second_attempting = threading.Event(), threading.Event(), threading.Event()
    original = importer._restore
    original_expire = importer.expire_previews
    calls = 0

    async def observe_expiry(*args: Any, **kwargs: Any) -> None:
        if entered.is_set():
            second_attempting.set()
        await original_expire(*args, **kwargs)

    monkeypatch.setattr(importer, "expire_previews", observe_expiry)

    async def paused(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        entered.set()
        await asyncio.to_thread(release.wait, 5)
        return await original(*args, **kwargs)

    monkeypatch.setattr(importer, "_restore", paused)
    user_id = env.query("SELECT id FROM users WHERE username='alpha'")[0][0]

    def run() -> Any:
        async def operation() -> Any:
            database = Database(env.database_url)
            try:
                async with database.sessions() as session:
                    return (
                        await importer.apply_preview(
                            session, user_id, result["id"], result["payload_sha256"], env.directory
                        )
                    ).model_dump()
            finally:
                await database.dispose()

        return asyncio.run(operation())

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(run)
        assert entered.wait(5)
        second = executor.submit(run)
        assert second_attempting.wait(5)
        assert not second.done()
        release.set()
        assert first.result(timeout=15) == second.result(timeout=15)
    assert calls == 1
    assert env.query("SELECT COUNT(*) FROM sites")[0][0] == 1


def test_stream_limit_and_tamper_cleanup(
    env: Environment, backup: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(importer, "MAX_PAYLOAD_BYTES", 10)
    response = env.client.post(
        "/api/portability/imports",
        content=iter([b"123456", b"78901"]),
        headers={**ORIGIN, "Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert not list((env.directory / "portability-imports").rglob("*.json"))
    monkeypatch.setattr(importer, "MAX_PAYLOAD_BYTES", 64 * 1024 * 1024)
    result = preview(env, backup)
    Path(env.query("SELECT payload_path FROM portability_imports")[0][0]).write_text("{}")
    response = env.client.post(
        f"/api/portability/imports/{result['id']}/apply",
        json={"expected_sha256": result["payload_sha256"]},
        headers=ORIGIN,
    )
    assert response.status_code == 409 and response.json()["detail"]["code"] == "checksum_mismatch"


def test_expiry_cleanup_never_overwrites_applied_receipt(
    env: Environment, backup: dict[str, Any]
) -> None:
    result = preview(env, backup)
    first = apply(env, result)
    env.execute("UPDATE portability_imports SET expires_at='2000-01-01 00:00:00'")
    assert env.client.get(f"/api/portability/imports/{result['id']}").json()["status"] == "applied"
    assert apply(env, result) == first
    assert env.query("SELECT status FROM portability_imports") == [("applied",)]


def test_preview_caps_samples_and_rejects_invalid_apply(
    env: Environment, backup: dict[str, Any]
) -> None:
    backup["tags"] += [{"key": f"new{i}", "name": f"标签{i}"} for i in range(30)]
    result = preview(env, backup)
    assert len(result["samples"]["tags_create"]) == 20
    expires = datetime.fromisoformat(result["expires_at"].replace("Z", "+00:00"))
    assert timedelta(hours=23, minutes=59) < expires - datetime.now(UTC) <= timedelta(hours=24)
    for invalid in (
        {},
        {"expected_sha256": "wrong"},
        {"expected_sha256": result["payload_sha256"], "extra": True},
    ):
        assert (
            env.client.post(
                f"/api/portability/imports/{result['id']}/apply", json=invalid, headers=ORIGIN
            ).status_code
            == 422
        )
    assert (
        env.client.post("/api/portability/imports", content=b"{}", headers=ORIGIN).status_code
        == 422
    )
    response = env.client.post(
        "/api/portability/imports",
        content=b'{"version":1,"version":1}',
        headers={**ORIGIN, "Content-Type": "application/json"},
    )
    assert response.status_code == 422


def test_new_site_usage_timestamps_preserve_offset_instants(
    env: Environment, backup: dict[str, Any]
) -> None:
    for field in ("created_at", "last_opened_at", "seen_at", "starred_at"):
        backup["sites"][0][field] = "2026-10-23T08:00:00+08:00"
    apply(env, preview(env, backup))
    exported = env.client.get("/api/portability/export").json()["sites"][0]
    for field in ("created_at", "last_opened_at", "seen_at", "starred_at"):
        assert exported[field] == "2026-10-23T00:00:00Z"


def test_json_exponent_overflow_is_rejected_before_staging(
    env: Environment, backup: dict[str, Any]
) -> None:
    backup["sites"][0]["meta"] = {"number": 0}
    payload = json.dumps(backup).replace('"number": 0', '"number": 1e400')
    response = env.client.post(
        "/api/portability/imports",
        content=payload,
        headers={**ORIGIN, "Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_format"
    assert env.query("SELECT COUNT(*) FROM portability_imports")[0][0] == 0
    assert env.query("SELECT COUNT(*) FROM sites")[0][0] == 0
    assert not list((env.directory / "portability-imports").rglob("*.json"))
