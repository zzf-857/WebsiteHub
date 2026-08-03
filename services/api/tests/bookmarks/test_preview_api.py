import asyncio
import hashlib
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.bookmarks import persistence
from webhub.bookmarks import queries as bookmark_queries
from webhub.bookmarks import routes as bookmark_routes
from webhub.bookmarks.models import ParsedBookmark, ParsedFolder
from webhub.config import Settings
from webhub.db.database import Database
from webhub.db.migrations import upgrade_database
from webhub.db.models import BookmarkImportJob, Site
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


def _similarity_events(
    host: str = "example.com",
    *,
    additional_hosts: tuple[str, ...] = (),
) -> list[ParsedFolder | ParsedBookmark]:
    events: list[ParsedFolder | ParsedBookmark] = [
        ParsedFolder(
            source_folder_id=1,
            parent_source_folder_id=None,
            source_order=1,
            source_sequence=1,
            title="AI 工具",
            folder_path=("AI 工具",),
            depth=1,
        ),
    ]
    for current_host in (host, *additional_hosts):
        for path, title in (
            ("/", "Example home"),
            ("/docs", "Example docs"),
            ("/pricing?ref=bookmark", "Example pricing"),
        ):
            position = len(events)
            events.append(
                ParsedBookmark(
                    position=position,
                    source_sequence=position + 1,
                    raw_url=f"https://{current_host}{path}",
                    title=title,
                    folder_path=("AI 工具",),
                    source_folder_id=1,
                    add_date=None,
                    last_modified=None,
                )
            )
    return events


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


async def _create_similarity_job(
    database: Database,
    user_id: str,
    *,
    key: str,
    host: str = "example.com",
    additional_hosts: tuple[str, ...] = (),
) -> str:
    source_sha256 = _source_digest(f"{user_id}:{key}")
    async with database.sessions() as session:
        created = await persistence.create_import(
            session,
            user_id,
            source_sha256=source_sha256,
            source_size_bytes=1_024,
            original_filename=f"{key}.html",
            idempotency_key=f"upload-similarity-{key}-request",
        )
    async with database.sessions() as session:
        run = await persistence.begin_parse_run(
            session,
            user_id,
            created.job_id,
            expected_job_version=1,
            idempotency_key=f"parse-similarity-{key}-request",
        )
    events = _similarity_events(host, additional_hosts=additional_hosts)
    async with database.sessions() as session:
        await persistence.append_parse_chunk(
            session,
            user_id,
            created.job_id,
            run.run_id,
            chunk_index=0,
            events=events,
        )
    async with database.sessions() as session:
        await persistence.finalize_parse_run(
            session,
            user_id,
            created.job_id,
            run.run_id,
            expected_job_version=2,
            completion=persistence.ParseCompletion(
                source_sha256=source_sha256,
                source_sequence_count=len(events),
                folder_count=1,
                occurrence_count=len(events) - 1,
            ),
        )
    return created.job_id


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
        "decision_version": 1,
        "similarity_cluster_count": 0,
        "similarity_candidate_count": 0,
        "similarity_decision_counts": {
            "unresolved": 0,
            "merge_to_homepage": 0,
            "keep_originals": 0,
        },
        "selected_merge_reduction_count": 0,
        "projected_create_count": 2,
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


def test_similarity_cluster_numbered_pagination_stays_on_page_after_decision(
    preview_environment: PreviewEnvironment,
) -> None:
    environment = preview_environment
    client = environment.client
    database = Database(environment.database_url)

    try:
        job_id = asyncio.run(
            _create_similarity_job(
                database,
                environment.alice_id,
                key="similarity-pagination",
                additional_hosts=("second.example",),
            )
        )
        base = f"/api/bookmark-imports/{job_id}"
        _use_token(client, environment.alice_token)

        first_page = client.get(
            f"{base}/preview/similarity-clusters",
            params={"limit": 1, "page": 1},
        )
        assert first_page.status_code == 200, first_page.text
        assert first_page.json()["page"] == 1
        assert first_page.json()["page_size"] == 1
        assert first_page.json()["total_count"] == 2
        assert first_page.json()["total_pages"] == 2
        assert first_page.json()["next_cursor"] is not None

        cursor_page = client.get(
            f"{base}/preview/similarity-clusters",
            params={"limit": 1, "cursor": first_page.json()["next_cursor"]},
        )
        assert cursor_page.status_code == 200, cursor_page.text
        assert cursor_page.json()["page"] == 2

        second_page = client.get(
            f"{base}/preview/similarity-clusters",
            params={"limit": 1, "page": 2},
        )
        assert second_page.status_code == 200, second_page.text
        second_payload = second_page.json()
        assert second_payload["page"] == 2
        assert second_payload["next_cursor"] is None
        second_cluster = second_payload["items"][0]

        mixed_pagination = client.get(
            f"{base}/preview/similarity-clusters",
            params={"limit": 1, "page": 2, "cursor": first_page.json()["next_cursor"]},
        )
        assert mixed_pagination.status_code == 422, mixed_pagination.text

        decided = client.put(
            f"{base}/preview/similarity-clusters/{second_cluster['id']}/decision",
            json={
                "expected_job_version": 3,
                "expected_decision_version": 1,
                "decision": "merge_to_homepage",
            },
            headers=ORIGIN,
        )
        assert decided.status_code == 200, decided.text
        assert decided.json()["decision_version"] == 2

        refreshed_page = client.get(
            f"{base}/preview/similarity-clusters",
            params={"limit": 1, "page": 2},
        )
        assert refreshed_page.status_code == 200, refreshed_page.text
        refreshed_payload = refreshed_page.json()
        assert refreshed_payload["page"] == 2
        assert refreshed_payload["total_pages"] == 2
        assert refreshed_payload["items"][0]["id"] == second_cluster["id"]
        assert refreshed_payload["items"][0]["decision"] == "merge_to_homepage"
        assert refreshed_payload["decision_version"] == 2

        filtered_page = client.get(
            f"{base}/preview/similarity-clusters",
            params={"limit": 1, "page": 1, "decision": "merge_to_homepage"},
        )
        assert filtered_page.status_code == 200, filtered_page.text
        assert filtered_page.json()["total_count"] == 1
        assert filtered_page.json()["total_pages"] == 1
    finally:
        asyncio.run(database.dispose())


def test_similarity_decisions_gate_atomic_apply_and_are_account_scoped(
    preview_environment: PreviewEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = preview_environment
    client = environment.client
    database = Database(environment.database_url)
    monkeypatch.setattr(
        bookmark_routes.ingestion_worker,
        "schedule_analysis",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bookmark_routes.ingestion_worker,
        "ensure_auto_backfill",
        lambda *_args, **_kwargs: False,
    )

    try:
        job_id = asyncio.run(
            _create_similarity_job(
                database,
                environment.alice_id,
                key="similarity-merge",
            )
        )
        base = f"/api/bookmark-imports/{job_id}"

        _use_token(client, environment.bob_token)
        assert client.get(f"{base}/preview/similarity-clusters").status_code == 404

        _use_token(client, environment.alice_token)
        summary = client.get(f"{base}/preview").json()
        assert summary["similarity_cluster_count"] == 1
        assert summary["similarity_candidate_count"] == 3
        assert summary["similarity_decision_counts"] == {
            "unresolved": 1,
            "merge_to_homepage": 0,
            "keep_originals": 0,
        }
        assert summary["projected_create_count"] == 3

        unresolved_apply = client.post(
            f"{base}/apply",
            json={"expected_job_version": 3, "expected_decision_version": 1},
            headers=ORIGIN,
        )
        assert unresolved_apply.status_code == 409, unresolved_apply.text

        cluster_page = client.get(
            f"{base}/preview/similarity-clusters",
            params={"limit": 1},
        )
        assert cluster_page.status_code == 200, cluster_page.text
        cluster_payload = cluster_page.json()
        assert cluster_payload["decision_version"] == 1
        assert cluster_payload["next_cursor"] is None
        assert cluster_payload["page"] == 1
        assert cluster_payload["page_size"] == 1
        assert cluster_payload["total_count"] == 1
        assert cluster_payload["total_pages"] == 1
        cluster = cluster_payload["items"][0]
        assert cluster["candidate_count"] == 3
        assert cluster["canonical"]["url"] == "https://example.com/"
        assert cluster["decision"] is None

        members = client.get(
            f"{base}/preview/similarity-clusters/{cluster['id']}/members",
            params={"limit": 2},
        )
        assert members.status_code == 200, members.text
        assert len(members.json()["items"]) == 2
        assert members.json()["next_cursor"] is not None

        decided = client.put(
            f"{base}/preview/similarity-clusters/{cluster['id']}/decision",
            json={
                "expected_job_version": 3,
                "expected_decision_version": 1,
                "decision": "merge_to_homepage",
            },
            headers=ORIGIN,
        )
        assert decided.status_code == 200, decided.text
        assert decided.json()["decision_version"] == 2
        assert decided.json()["similarity_decision_counts"]["unresolved"] == 0
        assert decided.json()["selected_merge_reduction_count"] == 2
        assert decided.json()["projected_create_count"] == 1

        stale_decision = client.put(
            f"{base}/preview/similarity-clusters/{cluster['id']}/decision",
            json={
                "expected_job_version": 3,
                "expected_decision_version": 1,
                "decision": "keep_originals",
            },
            headers=ORIGIN,
        )
        assert stale_decision.status_code == 409, stale_decision.text

        stale_apply = client.post(
            f"{base}/apply",
            json={"expected_job_version": 3, "expected_decision_version": 1},
            headers=ORIGIN,
        )
        assert stale_apply.status_code == 409, stale_apply.text

        applied = client.post(
            f"{base}/apply",
            json={"expected_job_version": 3, "expected_decision_version": 2},
            headers=ORIGIN,
        )
        assert applied.status_code == 200, applied.text
        assert applied.json() == {
            "job_id": job_id,
            "state": "completed",
            "job_version": 4,
            "total_candidates": 3,
            "created": 1,
            "skipped_existing": 0,
            "skipped_needs_review": 0,
            "merged_candidates": 2,
            "failed": 0,
        }
        replay = client.post(
            f"{base}/apply",
            json={"expected_job_version": 4, "expected_decision_version": 2},
            headers=ORIGIN,
        )
        assert replay.status_code == 409, replay.text

        originals_job_id = asyncio.run(
            _create_similarity_job(
                database,
                environment.alice_id,
                key="similarity-originals",
                host="originals.example",
            )
        )
        originals_base = f"/api/bookmark-imports/{originals_job_id}"
        kept = client.post(
            f"{originals_base}/preview/similarity-decisions/keep-originals",
            json={
                "expected_job_version": 3,
                "expected_decision_version": 1,
                "decision": "keep_originals",
            },
            headers=ORIGIN,
        )
        assert kept.status_code == 200, kept.text
        assert kept.json()["decision_version"] == 2
        assert kept.json()["similarity_decision_counts"] == {
            "unresolved": 0,
            "merge_to_homepage": 0,
            "keep_originals": 1,
        }
        originals_apply = client.post(
            f"{originals_base}/apply",
            json={"expected_job_version": 3, "expected_decision_version": 2},
            headers=ORIGIN,
        )
        assert originals_apply.status_code == 200, originals_apply.text
        assert originals_apply.json()["created"] == 3
        assert originals_apply.json()["merged_candidates"] == 0

        async def imported_urls() -> list[str]:
            async with database.sessions() as session:
                return list(
                    (
                        await session.scalars(
                            select(Site.identity_url)
                            .where(
                                Site.user_id == environment.alice_id,
                                Site.identity_url.in_(
                                    (
                                        "https://example.com/",
                                        "https://example.com/docs",
                                        "https://example.com/pricing?ref=bookmark",
                                    )
                                ),
                            )
                            .order_by(Site.identity_url)
                        )
                    ).all()
                )

        assert asyncio.run(imported_urls()) == ["https://example.com/"]
    finally:
        asyncio.run(database.dispose())


def test_apply_failure_rolls_back_library_and_job_claim(
    preview_environment: PreviewEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = preview_environment
    client = environment.client
    database = Database(environment.database_url)
    _use_token(client, environment.alice_token)

    try:
        job_id = asyncio.run(
            _create_similarity_job(
                database,
                environment.alice_id,
                key="similarity-rollback",
                host="rollback.example",
            )
        )
        resolved = client.post(
            f"/api/bookmark-imports/{job_id}/preview/similarity-decisions/keep-originals",
            json={
                "expected_job_version": 3,
                "expected_decision_version": 1,
                "decision": "keep_originals",
            },
            headers=ORIGIN,
        )
        assert resolved.status_code == 200, resolved.text

        async def fail_after_library_write(
            session: AsyncSession,
            user_id: str,
            _run_id: str,
        ) -> None:
            site = await session.scalar(
                select(Site).where(
                    Site.user_id == user_id,
                    Site.identity_url == "https://example.com/shared?q=1#keep",
                )
            )
            assert site is not None
            site.name = "must be rolled back"
            await session.flush()
            raise RuntimeError("injected apply failure")

        monkeypatch.setattr(bookmark_queries, "apply_candidates", fail_after_library_write)

        async def scenario() -> None:
            async with database.sessions() as session:
                with pytest.raises(RuntimeError, match="injected apply failure"):
                    await bookmark_queries.apply_import(
                        session,
                        environment.alice_id,
                        job_id,
                        expected_job_version=3,
                        expected_decision_version=2,
                    )
            async with database.sessions() as session:
                job = await session.scalar(
                    select(BookmarkImportJob).where(
                        BookmarkImportJob.user_id == environment.alice_id,
                        BookmarkImportJob.id == job_id,
                    )
                )
                site_name = await session.scalar(
                    select(Site.name).where(
                        Site.user_id == environment.alice_id,
                        Site.identity_url == "https://example.com/shared?q=1#keep",
                    )
                )
            assert job is not None
            assert (job.state, job.version) == ("parse_preview_ready", 3)
            assert site_name == "Existing shared bookmark"

        asyncio.run(scenario())
    finally:
        asyncio.run(database.dispose())


def test_concurrent_apply_claim_allows_only_one_commit(
    preview_environment: PreviewEnvironment,
) -> None:
    environment = preview_environment
    client = environment.client
    database = Database(environment.database_url)
    _use_token(client, environment.alice_token)

    try:
        job_id = asyncio.run(
            _create_similarity_job(
                database,
                environment.alice_id,
                key="similarity-concurrent",
                host="concurrent.example",
            )
        )
        resolved = client.post(
            f"/api/bookmark-imports/{job_id}/preview/similarity-decisions/keep-originals",
            json={
                "expected_job_version": 3,
                "expected_decision_version": 1,
                "decision": "keep_originals",
            },
            headers=ORIGIN,
        )
        assert resolved.status_code == 200, resolved.text

        async def apply_once() -> object:
            async with database.sessions() as session:
                return await bookmark_queries.apply_import(
                    session,
                    environment.alice_id,
                    job_id,
                    expected_job_version=3,
                    expected_decision_version=2,
                )

        async def scenario() -> list[object]:
            return list(
                await asyncio.gather(
                    apply_once(),
                    apply_once(),
                    return_exceptions=True,
                )
            )

        results = asyncio.run(scenario())
        successes = [result for result in results if not isinstance(result, BaseException)]
        conflicts = [
            result
            for result in results
            if isinstance(result, persistence.BookmarkPersistenceConflictError)
        ]
        assert len(successes) == 1
        assert len(conflicts) == 1
        assert successes[0].created == 3

        async def created_count() -> int:
            async with database.sessions() as session:
                return len(
                    (
                        await session.scalars(
                            select(Site.id).where(
                                Site.user_id == environment.alice_id,
                                Site.identity_url.like("https://concurrent.example/%"),
                            )
                        )
                    ).all()
                )

        assert asyncio.run(created_count()) == 3
    finally:
        asyncio.run(database.dispose())
