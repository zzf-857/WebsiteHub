import asyncio
import hashlib
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, update

from webhub.bookmarks import persistence
from webhub.bookmarks.models import ParsedBookmark, ParsedFolder
from webhub.config import Settings
from webhub.db.database import Database
from webhub.db.migrations import upgrade_database
from webhub.db.models import BookmarkImportJob
from webhub.main import create_app

COOKIE_NAME = "webhub_session"
ORIGIN = {"Origin": "http://testserver"}
PREVIEW_SUFFIXES = ("preview", "preview/folders", "preview/candidates", "preview/occurrences")


@dataclass(frozen=True, slots=True)
class PreviewEnvironment:
    client: TestClient
    database_url: str
    alice_id: str
    alice_token: str
    alice_job_id: str
    alice_run_id: str
    alice_source_sha256: str
    alice_other_job_id: str
    incomplete_job_id: str
    bob_id: str
    bob_token: str
    bob_job_id: str


def _register(client: TestClient, username: str) -> tuple[str, str]:
    response = client.post(
        "/api/auth/register",
        json={"username": username, "password": "a sufficiently secure password"},
        headers=ORIGIN,
    )
    assert response.status_code == 201, response.text
    token = response.cookies.get(COOKIE_NAME)
    assert token
    return str(response.json()["user"]["id"]), token


def _use_token(client: TestClient, token: str) -> None:
    client.cookies.clear()
    client.cookies.set(COOKIE_NAME, token)


def _events() -> list[ParsedFolder | ParsedBookmark]:
    return [
        ParsedFolder(
            source_folder_id=1,
            parent_source_folder_id=None,
            source_order=1,
            source_sequence=1,
            title="Root",
            folder_path=("Root",),
            depth=1,
        ),
        ParsedBookmark(
            position=1,
            source_sequence=2,
            raw_url="https://example.com/shared?q=1#keep",
            title="Short title",
            folder_path=("Root",),
            source_folder_id=1,
            add_date=10,
            last_modified=20,
        ),
        ParsedFolder(
            source_folder_id=2,
            parent_source_folder_id=1,
            source_order=2,
            source_sequence=3,
            title="Nested",
            folder_path=("Root", "Nested"),
            depth=2,
        ),
        ParsedBookmark(
            position=2,
            source_sequence=4,
            raw_url="https://example.com/shared?q=1#keep",
            title="A much longer shared title",
            folder_path=("Root", "Nested"),
            source_folder_id=2,
            add_date=11,
            last_modified=21,
        ),
        ParsedBookmark(
            position=3,
            source_sequence=5,
            raw_url="https://secure.example/path?token=secret#keep",
            title="Sensitive public bookmark",
            folder_path=("Root",),
            source_folder_id=1,
            add_date=12,
            last_modified=22,
        ),
        ParsedFolder(
            source_folder_id=3,
            parent_source_folder_id=None,
            source_order=3,
            source_sequence=6,
            title="Other",
            folder_path=("Other",),
            depth=1,
        ),
        ParsedBookmark(
            position=4,
            source_sequence=7,
            raw_url="http://localhost:3000/private",
            title="Local metadata only",
            folder_path=("Other",),
            source_folder_id=3,
            add_date=13,
            last_modified=23,
        ),
        ParsedBookmark(
            position=5,
            source_sequence=8,
            raw_url="file:///C:/private.txt",
            title="Unsupported file",
            folder_path=("Other",),
            source_folder_id=3,
            add_date=None,
            last_modified=None,
        ),
        ParsedBookmark(
            position=6,
            source_sequence=9,
            raw_url="https://",
            title="Invalid URL",
            folder_path=("Other",),
            source_folder_id=3,
            add_date=None,
            last_modified=None,
        ),
    ]


def _source_digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


async def _publish_run(
    database: Database,
    user_id: str,
    job_id: str,
    source_sha256: str,
    *,
    expected_job_version: int,
    key: str,
) -> persistence.ParsePreviewSummary:
    events = _events()
    async with database.sessions() as session:
        run = await persistence.begin_parse_run(
            session,
            user_id,
            job_id,
            expected_job_version=expected_job_version,
            idempotency_key=f"parse-preview-{key}-request",
        )
    async with database.sessions() as session:
        await persistence.append_parse_chunk(
            session,
            user_id,
            job_id,
            run.run_id,
            chunk_index=0,
            events=events[:3],
        )
    async with database.sessions() as session:
        await persistence.append_parse_chunk(
            session,
            user_id,
            job_id,
            run.run_id,
            chunk_index=1,
            events=events[3:],
        )
    async with database.sessions() as session:
        return await persistence.finalize_parse_run(
            session,
            user_id,
            job_id,
            run.run_id,
            expected_job_version=expected_job_version + 1,
            completion=persistence.ParseCompletion(
                source_sha256=source_sha256,
                source_sequence_count=9,
                folder_count=3,
                occurrence_count=6,
            ),
        )


async def _create_job(
    database: Database,
    user_id: str,
    *,
    key: str,
    complete: bool,
) -> tuple[str, str | None, str]:
    source_sha256 = _source_digest(f"{user_id}:{key}")
    async with database.sessions() as session:
        created = await persistence.create_import(
            session,
            user_id,
            source_sha256=source_sha256,
            source_size_bytes=1_024,
            original_filename=f"{key}.html",
            idempotency_key=f"upload-preview-{key}-request",
        )
    if not complete:
        return created.job_id, None, source_sha256
    preview = await _publish_run(
        database,
        user_id,
        created.job_id,
        source_sha256,
        expected_job_version=1,
        key=key,
    )
    return created.job_id, preview.run_id, source_sha256


@pytest.fixture
def preview_environment(tmp_path: Path) -> Iterator[PreviewEnvironment]:
    database_path = tmp_path / "main.sqlite3"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    settings = Settings(
        environment="test",
        database_url=database_url,
        data_directory=tmp_path,
    )
    upgrade_database(settings.database_url)
    with TestClient(create_app(settings=settings)) as client:
        alice_id, alice_token = _register(client, "alice")
        bob_id, bob_token = _register(client, "bob")
        _use_token(client, alice_token)
        existing_site = client.post(
            "/api/library/sites",
            json={
                "name": "Existing shared bookmark",
                "url": "https://example.com/shared?q=1#keep",
            },
            headers=ORIGIN,
        )
        assert existing_site.status_code == 201, existing_site.text
        database = Database(database_url)

        async def seed() -> tuple[str, str, str, str, str, str]:
            alice_job_id, alice_run_id, alice_source_sha256 = await _create_job(
                database,
                alice_id,
                key="alice-primary",
                complete=True,
            )
            alice_other_job_id, _, _ = await _create_job(
                database,
                alice_id,
                key="alice-other",
                complete=True,
            )
            incomplete_job_id, _, _ = await _create_job(
                database,
                alice_id,
                key="alice-incomplete",
                complete=False,
            )
            bob_job_id, _, _ = await _create_job(
                database,
                bob_id,
                key="bob-primary",
                complete=True,
            )
            assert alice_run_id is not None
            return (
                alice_job_id,
                alice_run_id,
                alice_source_sha256,
                alice_other_job_id,
                incomplete_job_id,
                bob_job_id,
            )

        try:
            (
                alice_job_id,
                alice_run_id,
                alice_source_sha256,
                alice_other_job_id,
                incomplete_job_id,
                bob_job_id,
            ) = asyncio.run(seed())
        finally:
            asyncio.run(database.dispose())

        yield PreviewEnvironment(
            client=client,
            database_url=database_url,
            alice_id=alice_id,
            alice_token=alice_token,
            alice_job_id=alice_job_id,
            alice_run_id=alice_run_id,
            alice_source_sha256=alice_source_sha256,
            alice_other_job_id=alice_other_job_id,
            incomplete_job_id=incomplete_job_id,
            bob_id=bob_id,
            bob_token=bob_token,
            bob_job_id=bob_job_id,
        )


def _preview_paths(job_id: str) -> list[str]:
    return [f"/api/bookmark-imports/{job_id}/{suffix}" for suffix in PREVIEW_SUFFIXES]


def test_preview_requires_login_is_account_scoped_and_requires_complete_current_run(
    preview_environment: PreviewEnvironment,
) -> None:
    environment = preview_environment
    client = environment.client

    client.cookies.clear()
    for path in _preview_paths(environment.alice_job_id):
        assert client.get(path).status_code == 401

    _use_token(client, environment.bob_token)
    for path in _preview_paths(environment.alice_job_id):
        assert client.get(path).status_code == 404

    _use_token(client, environment.alice_token)
    for path in _preview_paths(environment.incomplete_job_id):
        response = client.get(path)
        assert response.status_code == 409, response.text

    for path in _preview_paths(environment.alice_job_id):
        response = client.get(path)
        assert response.status_code == 200, response.text


def test_preview_success_responses_are_no_store(
    preview_environment: PreviewEnvironment,
) -> None:
    environment = preview_environment
    client = environment.client
    _use_token(client, environment.alice_token)

    for path in _preview_paths(environment.alice_job_id):
        response = client.get(path)
        assert response.status_code == 200, response.text
        assert "no-store" in response.headers.get("cache-control", "").casefold()


def test_preview_account_scoped_not_found_responses_are_no_store(
    preview_environment: PreviewEnvironment,
) -> None:
    environment = preview_environment
    client = environment.client
    _use_token(client, environment.bob_token)

    for path in _preview_paths(environment.alice_job_id):
        response = client.get(path)
        assert response.status_code == 404, response.text
        assert "no-store" in response.headers.get("cache-control", "").casefold()


def test_preview_summary_and_rows_expose_only_the_safe_read_contract(
    preview_environment: PreviewEnvironment,
) -> None:
    environment = preview_environment
    client = environment.client
    _use_token(client, environment.alice_token)
    base = f"/api/bookmark-imports/{environment.alice_job_id}/preview"

    summary = client.get(base)
    assert summary.status_code == 200, summary.text
    assert summary.json() == {
        "job_id": environment.alice_job_id,
        "run_id": environment.alice_run_id,
        "job_version": 3,
        "preview_version": 1,
        "source_sequence_count": 9,
        "folder_count": 3,
        "occurrence_count": 6,
        "candidate_count": 3,
        "occurrence_counts": {"accepted": 4, "invalid": 1, "unsupported": 1},
        "duplicate_occurrence_count": 1,
        "candidate_action_counts": {
            "create": 2,
            "skip_existing": 1,
            "merge_missing_metadata": 0,
            "reject": 0,
            "needs_review": 0,
        },
        "metadata_only_candidate_count": 1,
        "sensitive_candidate_count": 1,
    }

    folders = client.get(f"{base}/folders").json()["items"]
    assert [folder["source_sequence"] for folder in folders] == [1, 3, 6]
    assert [folder["display_path"] for folder in folders] == [
        ["Root"],
        ["Root", "Nested"],
        ["Other"],
    ]
    assert folders[1]["parent_id"] == folders[0]["id"]
    assert folders[0]["parent_id"] is None
    assert folders[2]["parent_id"] is None

    candidates = client.get(f"{base}/candidates").json()["items"]
    assert [candidate["first_source_sequence"] for candidate in candidates] == [2, 5, 7]
    assert candidates[0]["title"] == "A much longer shared title"
    assert candidates[0]["occurrence_count"] == 2
    assert candidates[0]["proposed_action"] == "skip_existing"
    assert candidates[1]["has_sensitive_url"] is True
    assert candidates[2]["fetch_policy"] == "export_metadata_only"
    assert all(candidate["identity_url"] for candidate in candidates)
    for candidate in candidates:
        assert {"identity_hash", "user_id", "run_id"}.isdisjoint(candidate)

    occurrences = client.get(f"{base}/occurrences").json()["items"]
    assert [occurrence["source_sequence"] for occurrence in occurrences] == [2, 4, 5, 7, 8, 9]
    assert occurrences[0]["title"] == "Short title"
    assert occurrences[0]["url"] == "https://example.com/shared?q=1#keep"
    assert occurrences[0]["add_date"] == 10
    assert occurrences[3]["fetch_policy"] == "export_metadata_only"
    assert occurrences[4]["validation_status"] == "unsupported"
    assert occurrences[4]["reason_code"] == "unsupported_scheme:file"
    assert occurrences[4]["fetch_policy"] is None
    assert occurrences[5]["validation_status"] == "invalid"
    for occurrence in occurrences:
        assert {"user_id", "run_id", "source_occurrence_key"}.isdisjoint(occurrence)


def test_preview_collections_use_stable_keyset_pagination_and_validate_limits(
    preview_environment: PreviewEnvironment,
) -> None:
    environment = preview_environment
    client = environment.client
    _use_token(client, environment.alice_token)
    base = f"/api/bookmark-imports/{environment.alice_job_id}/preview"
    expected_sequences = {
        "folders": [1, 3, 6],
        "candidates": [2, 5, 7],
        "occurrences": [2, 4, 5, 7, 8, 9],
    }
    sequence_fields = {
        "folders": "source_sequence",
        "candidates": "first_source_sequence",
        "occurrences": "source_sequence",
    }

    for endpoint, expected in expected_sequences.items():
        cursor = None
        ids: list[str] = []
        sequences: list[int] = []
        while True:
            params: dict[str, str | int] = {"limit": 1}
            if cursor is not None:
                params["cursor"] = cursor
            response = client.get(f"{base}/{endpoint}", params=params)
            assert response.status_code == 200, response.text
            payload = response.json()
            ids.extend(item["id"] for item in payload["items"])
            sequences.extend(item[sequence_fields[endpoint]] for item in payload["items"])
            cursor = payload["next_cursor"]
            if cursor is None:
                break
        assert sequences == expected
        assert len(ids) == len(set(ids)) == len(expected)
        assert client.get(f"{base}/{endpoint}", params={"limit": 0}).status_code == 422
        assert client.get(f"{base}/{endpoint}", params={"limit": 101}).status_code == 422

    overlong_id = "x" * 129
    assert client.get(f"/api/bookmark-imports/{overlong_id}/preview").status_code == 422
    assert client.get(f"{base}/folders", params={"parent_id": overlong_id}).status_code == 422
    assert client.get(f"{base}/occurrences", params={"folder_id": overlong_id}).status_code == 422


def test_candidate_cursor_uses_composite_index_range_seek(
    preview_environment: PreviewEnvironment,
) -> None:
    environment = preview_environment
    client = environment.client
    _use_token(client, environment.alice_token)
    endpoint = f"/api/bookmark-imports/{environment.alice_job_id}/preview/candidates"
    first_page = client.get(endpoint, params={"limit": 1}).json()
    captured: list[tuple[str, tuple[object, ...]]] = []
    sync_engine = client.app.state.database.engine.sync_engine

    def capture_page_query(
        _connection: object,
        _cursor: object,
        statement: str,
        parameters: tuple[object, ...],
        _context: object,
        _executemany: bool,
    ) -> None:
        if "ORDER BY bookmark_staging_candidates.first_source_sequence" in statement:
            captured.append((statement, parameters))

    event.listen(sync_engine, "before_cursor_execute", capture_page_query)
    try:
        second_page = client.get(
            endpoint,
            params={"limit": 1, "cursor": first_page["next_cursor"]},
        )
    finally:
        event.remove(sync_engine, "before_cursor_execute", capture_page_query)

    assert second_page.status_code == 200, second_page.text
    assert captured
    statement, parameters = captured[-1]
    database_path = Path(environment.database_url.removeprefix("sqlite+aiosqlite:///"))
    with sqlite3.connect(database_path) as connection:
        plan = connection.execute(f"EXPLAIN QUERY PLAN {statement}", parameters).fetchall()
    details = " ".join(str(row[3]) for row in plan).replace(" ", "")
    assert "ix_bookmark_staging_candidates_user_run_sequence_id" in details
    assert "(first_source_sequence,id)>(?,?)" in details


def test_preview_cursors_are_tamper_detected_and_bound_to_scope(
    preview_environment: PreviewEnvironment,
) -> None:
    environment = preview_environment
    client = environment.client
    _use_token(client, environment.alice_token)
    base = f"/api/bookmark-imports/{environment.alice_job_id}/preview"
    first_page = client.get(f"{base}/folders", params={"limit": 1}).json()
    cursor = str(first_page["next_cursor"])
    root_id = str(first_page["items"][0]["id"])

    assert (
        client.get(
            f"{base}/folders",
            params={"limit": 1, "parent_id": root_id, "cursor": cursor},
        ).status_code
        == 422
    )
    assert client.get(f"{base}/candidates", params={"cursor": cursor}).status_code == 422
    assert (
        client.get(
            f"/api/bookmark-imports/{environment.alice_other_job_id}/preview/folders",
            params={"cursor": cursor},
        ).status_code
        == 422
    )

    index = len(cursor) // 2
    replacement = "A" if cursor[index] != "A" else "B"
    tampered = f"{cursor[:index]}{replacement}{cursor[index + 1 :]}"
    assert client.get(f"{base}/folders", params={"cursor": tampered}).status_code == 422

    _use_token(client, environment.bob_token)
    assert (
        client.get(
            f"/api/bookmark-imports/{environment.bob_job_id}/preview/folders",
            params={"cursor": cursor},
        ).status_code
        == 422
    )

    _use_token(client, environment.alice_token)
    occurrence_cursor = client.get(f"{base}/occurrences", params={"limit": 1}).json()["next_cursor"]
    assert (
        client.get(
            f"{base}/occurrences",
            params={"validation_status": "accepted", "cursor": occurrence_cursor},
        ).status_code
        == 422
    )


def test_preview_cursor_expires_when_the_current_run_switches(
    preview_environment: PreviewEnvironment,
) -> None:
    environment = preview_environment
    client = environment.client
    _use_token(client, environment.alice_token)
    base = f"/api/bookmark-imports/{environment.alice_job_id}/preview"
    old_cursor = client.get(f"{base}/occurrences", params={"limit": 1}).json()["next_cursor"]

    database = Database(environment.database_url)
    try:
        replacement = asyncio.run(
            _publish_run(
                database,
                environment.alice_id,
                environment.alice_job_id,
                environment.alice_source_sha256,
                expected_job_version=3,
                key="alice-replacement",
            )
        )
    finally:
        asyncio.run(database.dispose())

    assert replacement.run_id != environment.alice_run_id
    expired = client.get(f"{base}/occurrences", params={"cursor": old_cursor})
    assert expired.status_code == 422, expired.text
    summary = client.get(base)
    assert summary.status_code == 200
    assert summary.json()["run_id"] == replacement.run_id
    assert summary.json()["preview_version"] == 2


def test_preview_cursor_expires_when_preview_version_changes_without_a_run_switch(
    preview_environment: PreviewEnvironment,
) -> None:
    environment = preview_environment
    client = environment.client
    _use_token(client, environment.alice_token)
    base = f"/api/bookmark-imports/{environment.alice_job_id}/preview"
    old_cursor = client.get(f"{base}/candidates", params={"limit": 1}).json()["next_cursor"]

    database = Database(environment.database_url)

    async def advance_preview_version() -> None:
        async with database.sessions() as session:
            advanced = await session.execute(
                update(BookmarkImportJob)
                .where(
                    BookmarkImportJob.user_id == environment.alice_id,
                    BookmarkImportJob.id == environment.alice_job_id,
                    BookmarkImportJob.preview_version == 1,
                )
                .values(preview_version=BookmarkImportJob.preview_version + 1)
            )
            assert advanced.rowcount == 1
            await session.commit()

    try:
        asyncio.run(advance_preview_version())
    finally:
        asyncio.run(database.dispose())

    expired = client.get(f"{base}/candidates", params={"cursor": old_cursor})
    assert expired.status_code == 422, expired.text
    summary = client.get(base)
    assert summary.status_code == 200
    assert summary.json()["run_id"] == environment.alice_run_id
    assert summary.json()["preview_version"] == 2
