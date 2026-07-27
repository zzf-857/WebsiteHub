import asyncio
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from webhub.bookmarks import persistence
from webhub.bookmarks.models import ParserStats
from webhub.bookmarks.parser import iter_netscape_events
from webhub.config import Settings
from webhub.db.database import Database
from webhub.db.migrations import upgrade_database
from webhub.db.models import (
    BookmarkImportCheckpoint,
    BookmarkImportCurrentRun,
    BookmarkImportJob,
    BookmarkImportRun,
    BookmarkImportSnapshot,
    BookmarkStagingCandidate,
    BookmarkStagingCandidateFolder,
    BookmarkStagingCandidateOccurrence,
    BookmarkStagingCandidateSiteMatch,
    BookmarkStagingFolder,
    BookmarkStagingOccurrence,
    Site,
)
from webhub.main import create_app

COOKIE_NAME = "webhub_session"
ORIGIN = {"Origin": "http://testserver"}
SOURCE_URL = "https://example.com/shared?q=1#keep"


@pytest.fixture
def persistence_environment(tmp_path: Path) -> Iterator[tuple[TestClient, Path]]:
    database_path = tmp_path / "main.sqlite3"
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        data_directory=tmp_path,
    )
    upgrade_database(settings.database_url)
    with TestClient(create_app(settings=settings)) as client:
        yield client, database_path


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


def _bookmark_events(path: Path) -> tuple[list[object], ParserStats]:
    path.write_text(
        """<!DOCTYPE NETSCAPE-Bookmark-file-1>
<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">
<DL><p>
  <DT><H3>Root</H3><DL><p>
    <DT><A HREF="https://example.com/shared?q=1#keep">Short</A>
    <DT><H3>Nested</H3><DL><p>
      <DT><A HREF="https://example.com/shared?q=1#keep">A much longer shared title</A>
    </DL><p>
    <DT><A HREF="https://example.com/shared?q=2#keep">Query variant</A>
    <DT><A HREF="file:///C:/private.txt">Unsupported file</A>
  </DL><p>
</DL><p>
""",
        encoding="utf-8",
    )
    stats = ParserStats()
    events = list(iter_netscape_events(path, stats=stats, chunk_size=7))
    return events, stats


def test_receiving_import_is_queued_with_cas_and_replays_after_transition(
    persistence_environment: tuple[TestClient, Path],
) -> None:
    client, database_path = persistence_environment
    user_id, _ = _register(client, "receiving-import-user")
    database = Database(f"sqlite+aiosqlite:///{database_path.as_posix()}")

    async def scenario() -> None:
        async with database.sessions() as session:
            receiving = await persistence.create_import(
                session,
                user_id,
                source_sha256="a" * 64,
                source_size_bytes=128,
                original_filename="bookmarks.html",
                idempotency_key="receiving-upload-request-0001",
                detected_encoding="UTF-8",
                ready_for_parse=False,
            )
        assert receiving.state == "receiving"
        assert receiving.job_version == 1
        assert receiving.storage_key == (
            f"bookmark-imports/{user_id}/{receiving.snapshot_id}/source.html"
        )
        assert not receiving.replayed

        async with database.sessions() as session:
            replayed_receiving = await persistence.create_import(
                session,
                user_id,
                source_sha256="a" * 64,
                source_size_bytes=128,
                original_filename="renamed.html",
                idempotency_key="receiving-upload-request-0001",
                detected_encoding="utf-8",
                ready_for_parse=False,
            )
            snapshot = await session.get(BookmarkImportSnapshot, receiving.snapshot_id)
        assert replayed_receiving.replayed
        assert replayed_receiving.job_id == receiving.job_id
        assert replayed_receiving.state == "receiving"
        assert snapshot is not None
        assert snapshot.detected_encoding == "utf-8"

        async with database.sessions() as session:
            queued = await persistence.queue_import_for_parse(
                session,
                user_id,
                receiving.job_id,
                expected_job_version=1,
            )
        assert queued.state == "queued_parse"
        assert queued.job_version == 2
        assert not queued.replayed

        async with database.sessions() as session:
            replayed_queue = await persistence.queue_import_for_parse(
                session,
                user_id,
                receiving.job_id,
                expected_job_version=1,
            )
        assert replayed_queue.state == "queued_parse"
        assert replayed_queue.job_version == 2
        assert replayed_queue.replayed
        assert replayed_queue.storage_key == receiving.storage_key

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(database.dispose())


def test_account_scoped_staging_is_idempotent_and_switches_only_complete_runs(
    persistence_environment: tuple[TestClient, Path],
    tmp_path: Path,
) -> None:
    client, database_path = persistence_environment
    alice_id, alice_token = _register(client, "alice")
    bob_id, bob_token = _register(client, "bob")
    _use_token(client, bob_token)
    bob_site = client.post(
        "/api/library/sites",
        json={"name": "Bob only", "url": "https://bob-only.example/"},
        headers=ORIGIN,
    )
    assert bob_site.status_code == 201, bob_site.text
    bob_site_id = str(bob_site.json()["id"])
    _use_token(client, alice_token)
    existing_site = client.post(
        "/api/library/sites",
        json={
            "name": "Keep my title",
            "url": SOURCE_URL,
            "description": "Do not overwrite this description",
            "pinned": True,
        },
        headers=ORIGIN,
    )
    assert existing_site.status_code == 201, existing_site.text
    existing_site_payload = existing_site.json()
    removable_site = client.post(
        "/api/library/sites",
        json={
            "name": "Removable match",
            "url": "https://example.com/shared?q=2#keep",
        },
        headers=ORIGIN,
    )
    assert removable_site.status_code == 201, removable_site.text
    removable_site_id = str(removable_site.json()["id"])
    events, parser_stats = _bookmark_events(tmp_path / "bookmarks.html")
    assert len(events) == 6
    assert parser_stats.source_sha256

    database = Database(f"sqlite+aiosqlite:///{database_path.as_posix()}")

    async def scenario() -> tuple[str, str, str]:
        async with database.sessions() as session:
            first_import = await persistence.create_import(
                session,
                alice_id,
                source_sha256=parser_stats.source_sha256,
                source_size_bytes=parser_stats.source_size_bytes,
                original_filename="../private\\bookmarks.html",
                idempotency_key="upload-request-00000001",
            )
        assert first_import.job_version == 1
        assert first_import.state == "queued_parse"
        assert not first_import.replayed

        async with database.sessions() as session:
            replayed_import = await persistence.create_import(
                session,
                alice_id,
                source_sha256=parser_stats.source_sha256,
                source_size_bytes=parser_stats.source_size_bytes,
                original_filename="bookmarks.html",
                idempotency_key="upload-request-00000001",
            )
        assert replayed_import.replayed
        assert replayed_import.job_id == first_import.job_id
        assert replayed_import.snapshot_id == first_import.snapshot_id

        async with database.sessions() as session:
            with pytest.raises(
                persistence.BookmarkPersistenceConflictError,
                match="已绑定其他书签文件",
            ):
                await persistence.create_import(
                    session,
                    alice_id,
                    source_sha256="f" * 64,
                    source_size_bytes=parser_stats.source_size_bytes,
                    original_filename="other.html",
                    idempotency_key="upload-request-00000001",
                )

        async with database.sessions() as session:
            explicit_duplicate = await persistence.create_import(
                session,
                alice_id,
                source_sha256=parser_stats.source_sha256,
                source_size_bytes=parser_stats.source_size_bytes,
                original_filename="bookmarks.html",
                idempotency_key="upload-request-00000002",
            )
        assert explicit_duplicate.snapshot_id != first_import.snapshot_id

        async with database.sessions() as session:
            session.add(
                BookmarkImportJob(
                    user_id=bob_id,
                    snapshot_id=first_import.snapshot_id,
                    state="queued_parse",
                    parser_version="test-parser",
                    normalizer_version="test-normalizer",
                    skill_version="test-skill",
                )
            )
            with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
                await session.commit()
            await session.rollback()

        async with database.sessions() as session:
            alice_same_source = await persistence.find_same_source(
                session,
                alice_id,
                parser_stats.source_sha256,
            )
            bob_same_source = await persistence.find_same_source(
                session,
                bob_id,
                parser_stats.source_sha256,
            )
        assert len(alice_same_source) == 2
        assert bob_same_source == []

        async with database.sessions() as session:
            first_run = await persistence.begin_parse_run(
                session,
                alice_id,
                first_import.job_id,
                expected_job_version=1,
                idempotency_key="parse-run-request-0001",
            )
        assert first_run.job_version == 2

        async with database.sessions() as session:
            replayed_run = await persistence.begin_parse_run(
                session,
                alice_id,
                first_import.job_id,
                expected_job_version=1,
                idempotency_key="parse-run-request-0001",
            )
        assert replayed_run.replayed
        assert replayed_run.run_id == first_run.run_id

        async with database.sessions() as session:
            with pytest.raises(persistence.BookmarkPersistenceNotFoundError):
                await persistence.begin_parse_run(
                    session,
                    bob_id,
                    first_import.job_id,
                    expected_job_version=2,
                    idempotency_key="bob-parse-request-0001",
                )

        async with database.sessions() as session:
            first_chunk = await persistence.append_parse_chunk(
                session,
                alice_id,
                first_import.job_id,
                first_run.run_id,
                chunk_index=0,
                events=events[:3],  # type: ignore[arg-type]
            )
        assert not first_chunk.replayed

        async with database.sessions() as session:
            replayed_chunk = await persistence.append_parse_chunk(
                session,
                alice_id,
                first_import.job_id,
                first_run.run_id,
                chunk_index=0,
                events=events[:3],  # type: ignore[arg-type]
            )
        assert replayed_chunk.replayed
        assert replayed_chunk.payload_hash == first_chunk.payload_hash

        async with database.sessions() as session:
            with pytest.raises(
                persistence.BookmarkPersistenceConflictError,
                match="绑定不同内容",
            ):
                await persistence.append_parse_chunk(
                    session,
                    alice_id,
                    first_import.job_id,
                    first_run.run_id,
                    chunk_index=0,
                    events=events[1:3],  # type: ignore[arg-type]
                )

        async with database.sessions() as session:
            await persistence.append_parse_chunk(
                session,
                alice_id,
                first_import.job_id,
                first_run.run_id,
                chunk_index=1,
                events=events[3:],  # type: ignore[arg-type]
            )

        completion = persistence.ParseCompletion(
            source_sha256=parser_stats.source_sha256,
            source_sequence_count=6,
            folder_count=2,
            occurrence_count=4,
        )
        async with database.sessions() as session:
            first_preview = await persistence.finalize_parse_run(
                session,
                alice_id,
                first_import.job_id,
                first_run.run_id,
                expected_job_version=2,
                completion=completion,
            )
        assert first_preview.job_version == 3
        assert first_preview.preview_version == 1
        assert first_preview.candidate_count == 2

        async with database.sessions() as session:
            replayed_preview = await persistence.finalize_parse_run(
                session,
                alice_id,
                first_import.job_id,
                first_run.run_id,
                expected_job_version=2,
                completion=completion,
            )
        assert replayed_preview == first_preview

        async with database.sessions() as session:
            late_replay = await persistence.append_parse_chunk(
                session,
                alice_id,
                first_import.job_id,
                first_run.run_id,
                chunk_index=0,
                events=events[:3],  # type: ignore[arg-type]
            )
        assert late_replay.replayed

        async with database.sessions() as session:
            occurrence_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(BookmarkStagingOccurrence)
                    .where(
                        BookmarkStagingOccurrence.user_id == alice_id,
                        BookmarkStagingOccurrence.run_id == first_run.run_id,
                    )
                )
                or 0
            )
            candidate_link_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(BookmarkStagingCandidateOccurrence)
                    .where(
                        BookmarkStagingCandidateOccurrence.user_id == alice_id,
                        BookmarkStagingCandidateOccurrence.run_id == first_run.run_id,
                    )
                )
                or 0
            )
            folder_projection_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(BookmarkStagingCandidateFolder)
                    .where(
                        BookmarkStagingCandidateFolder.user_id == alice_id,
                        BookmarkStagingCandidateFolder.run_id == first_run.run_id,
                    )
                )
                or 0
            )
            shared_candidate = await session.scalar(
                select(BookmarkStagingCandidate).where(
                    BookmarkStagingCandidate.user_id == alice_id,
                    BookmarkStagingCandidate.run_id == first_run.run_id,
                    BookmarkStagingCandidate.identity_url == SOURCE_URL,
                )
            )
        assert occurrence_count == 4
        assert candidate_link_count == 3
        assert folder_projection_count == 3
        assert shared_candidate is not None
        assert shared_candidate.occurrence_count == 2
        assert shared_candidate.display_title == "A much longer shared title"
        assert shared_candidate.proposed_action == "skip_existing"

        async with database.sessions() as session:
            session.add(
                BookmarkImportCheckpoint(
                    user_id=alice_id,
                    run_id=first_run.run_id,
                    phase="classification",
                    chunk_index=0,
                    idempotency_key_hash="a" * 64,
                    input_hash="b" * 64,
                    state="complete",
                    processed_count=2,
                )
            )
            await session.execute(
                update(BookmarkStagingCandidate)
                .where(
                    BookmarkStagingCandidate.user_id == alice_id,
                    BookmarkStagingCandidate.run_id == first_run.run_id,
                    BookmarkStagingCandidate.identity_url == SOURCE_URL,
                )
                .values(proposed_action="needs_review")
            )
            await session.execute(
                delete(Site).where(
                    Site.user_id == alice_id,
                    Site.id == removable_site_id,
                )
            )
            await session.commit()

        async with database.sessions() as session:
            classification_checkpoint_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(BookmarkImportCheckpoint)
                    .where(
                        BookmarkImportCheckpoint.user_id == alice_id,
                        BookmarkImportCheckpoint.run_id == first_run.run_id,
                        BookmarkImportCheckpoint.phase == "classification",
                    )
                )
                or 0
            )
            remaining_removable_matches = int(
                await session.scalar(
                    select(func.count())
                    .select_from(BookmarkStagingCandidateSiteMatch)
                    .where(
                        BookmarkStagingCandidateSiteMatch.user_id == alice_id,
                        BookmarkStagingCandidateSiteMatch.run_id == first_run.run_id,
                        BookmarkStagingCandidateSiteMatch.site_id == removable_site_id,
                    )
                )
                or 0
            )
            query_candidate_id = await session.scalar(
                select(BookmarkStagingCandidate.id).where(
                    BookmarkStagingCandidate.user_id == alice_id,
                    BookmarkStagingCandidate.run_id == first_run.run_id,
                    BookmarkStagingCandidate.identity_url == "https://example.com/shared?q=2#keep",
                )
            )
        assert classification_checkpoint_count == 1
        assert remaining_removable_matches == 0
        assert query_candidate_id is not None

        async with database.sessions() as session:
            session.add(
                BookmarkStagingCandidateSiteMatch(
                    user_id=alice_id,
                    run_id=first_run.run_id,
                    candidate_id=query_candidate_id,
                    site_id=bob_site_id,
                    site_version=1,
                )
            )
            with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
                await session.commit()
            await session.rollback()

        for mutation in (
            update(BookmarkImportCheckpoint)
            .where(
                BookmarkImportCheckpoint.user_id == alice_id,
                BookmarkImportCheckpoint.run_id == first_run.run_id,
                BookmarkImportCheckpoint.phase == "parse",
            )
            .values(processed_count=999),
            update(BookmarkStagingOccurrence)
            .where(
                BookmarkStagingOccurrence.user_id == alice_id,
                BookmarkStagingOccurrence.run_id == first_run.run_id,
            )
            .values(raw_title="tampered"),
            delete(BookmarkStagingOccurrence).where(
                BookmarkStagingOccurrence.user_id == alice_id,
                BookmarkStagingOccurrence.run_id == first_run.run_id,
            ),
        ):
            async with database.sessions() as session:
                with pytest.raises(IntegrityError, match="immutable"):
                    await session.execute(mutation)
                    await session.commit()
                await session.rollback()

        async with database.sessions() as session:
            with pytest.raises(IntegrityError, match="candidate structure is immutable"):
                await session.execute(
                    update(BookmarkStagingCandidate)
                    .where(
                        BookmarkStagingCandidate.user_id == alice_id,
                        BookmarkStagingCandidate.run_id == first_run.run_id,
                    )
                    .values(identity_url="https://tampered.example/")
                )
                await session.commit()
            await session.rollback()

        async with database.sessions() as session:
            session.add(
                BookmarkStagingCandidate(
                    user_id=alice_id,
                    run_id=first_run.run_id,
                    identity_url="https://late.example/",
                    identity_hash="c" * 64,
                    display_title="Late candidate",
                    host="late.example",
                    fetch_policy="public_revalidation_required",
                    has_sensitive_url=False,
                    proposed_action="create",
                    occurrence_count=1,
                    first_source_sequence=1,
                )
            )
            with pytest.raises(IntegrityError, match="candidate structure is immutable"):
                await session.commit()
            await session.rollback()

        async with database.sessions() as session:
            failed_run = await persistence.begin_parse_run(
                session,
                alice_id,
                first_import.job_id,
                expected_job_version=3,
                idempotency_key="parse-run-request-0002",
            )
        async with database.sessions() as session:
            await persistence.append_parse_chunk(
                session,
                alice_id,
                first_import.job_id,
                failed_run.run_id,
                chunk_index=0,
                events=events,  # type: ignore[arg-type]
            )
        async with database.sessions() as session:
            await persistence.fail_parse_run(
                session,
                alice_id,
                first_import.job_id,
                failed_run.run_id,
                expected_job_version=4,
                failure_code="worker_restart",
            )
        async with database.sessions() as session:
            await persistence.fail_parse_run(
                session,
                alice_id,
                first_import.job_id,
                failed_run.run_id,
                expected_job_version=4,
                failure_code="worker_restart",
            )
        async with database.sessions() as session:
            after_failure = await persistence.get_current_preview_summary(
                session,
                alice_id,
                first_import.job_id,
            )
            failed_job = await session.scalar(
                select(BookmarkImportJob).where(
                    BookmarkImportJob.user_id == alice_id,
                    BookmarkImportJob.id == first_import.job_id,
                )
            )
        assert after_failure.run_id == first_run.run_id
        assert after_failure.job_version == 5
        assert failed_job is not None and failed_job.completed_at is not None

        async with database.sessions() as session:
            final_run = await persistence.begin_parse_run(
                session,
                alice_id,
                first_import.job_id,
                expected_job_version=5,
                idempotency_key="parse-run-request-0003",
            )
        async with database.sessions() as session:
            await persistence.append_parse_chunk(
                session,
                alice_id,
                first_import.job_id,
                final_run.run_id,
                chunk_index=0,
                events=events,  # type: ignore[arg-type]
            )
        async with database.sessions() as session:
            final_preview = await persistence.finalize_parse_run(
                session,
                alice_id,
                first_import.job_id,
                final_run.run_id,
                expected_job_version=6,
                completion=completion,
            )
        assert final_preview.run_id == final_run.run_id
        assert final_preview.job_version == 7

        async with database.sessions() as session:
            current_run_id = await session.scalar(
                select(BookmarkImportCurrentRun.run_id).where(
                    BookmarkImportCurrentRun.user_id == alice_id,
                    BookmarkImportCurrentRun.job_id == first_import.job_id,
                )
            )
            job = await session.scalar(
                select(BookmarkImportJob).where(
                    BookmarkImportJob.user_id == alice_id,
                    BookmarkImportJob.id == first_import.job_id,
                )
            )
        assert current_run_id == final_run.run_id
        assert job is not None and job.preview_version == 2

        async with database.sessions() as session:
            await session.execute(
                delete(BookmarkImportJob).where(
                    BookmarkImportJob.user_id == alice_id,
                    BookmarkImportJob.id == first_import.job_id,
                )
            )
            await session.commit()

        async with database.sessions() as session:
            remaining_runs = int(
                await session.scalar(
                    select(func.count())
                    .select_from(BookmarkImportRun)
                    .where(BookmarkImportRun.job_id == first_import.job_id)
                )
                or 0
            )
            remaining_checkpoints = int(
                await session.scalar(
                    select(func.count())
                    .select_from(BookmarkImportCheckpoint)
                    .where(BookmarkImportCheckpoint.user_id == alice_id)
                )
                or 0
            )
            remaining_occurrences = int(
                await session.scalar(
                    select(func.count())
                    .select_from(BookmarkStagingOccurrence)
                    .where(BookmarkStagingOccurrence.user_id == alice_id)
                )
                or 0
            )
            remaining_current_runs = int(
                await session.scalar(
                    select(func.count())
                    .select_from(BookmarkImportCurrentRun)
                    .where(BookmarkImportCurrentRun.user_id == alice_id)
                )
                or 0
            )
        assert remaining_runs == 0
        assert remaining_checkpoints == 0
        assert remaining_occurrences == 0
        assert remaining_current_runs == 0
        return first_import.job_id, first_run.run_id, final_run.run_id

    try:
        job_id, first_run_id, final_run_id = asyncio.run(scenario())
    finally:
        asyncio.run(database.dispose())

    _use_token(client, alice_token)
    stored_site = client.get(f"/api/library/sites/{existing_site_payload['id']}")
    assert stored_site.status_code == 200
    assert stored_site.json()["name"] == "Keep my title"
    assert stored_site.json()["description"] == "Do not overwrite this description"
    assert stored_site.json()["pinned"] is True
    assert job_id
    assert first_run_id != final_run_id


def test_begin_parse_run_replay_reports_the_committed_job_version(
    persistence_environment: tuple[TestClient, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, database_path = persistence_environment
    user_id, _ = _register(client, "run-replay-user")
    database = Database(f"sqlite+aiosqlite:///{database_path.as_posix()}")

    async def scenario() -> None:
        async with database.sessions() as session:
            import_job = await persistence.create_import(
                session,
                user_id,
                source_sha256="d" * 64,
                source_size_bytes=128,
                original_filename="bookmarks.html",
                idempotency_key="run-replay-upload-0001",
            )

        creator_read_job = asyncio.Event()
        retry_read_job = asyncio.Event()
        creator_finished = asyncio.Event()
        original_owned_job = persistence._common._owned_job

        async with (
            database.sessions() as creator_session,
            database.sessions() as retry_session,
        ):

            async def coordinated_owned_job(
                session: object,
                scoped_user_id: str,
                job_id: str,
            ) -> BookmarkImportJob:
                job = await original_owned_job(  # type: ignore[arg-type]
                    session,
                    scoped_user_id,
                    job_id,
                )
                if session is creator_session:
                    creator_read_job.set()
                    await asyncio.wait_for(retry_read_job.wait(), timeout=5)
                elif session is retry_session:
                    retry_read_job.set()
                    await asyncio.wait_for(creator_read_job.wait(), timeout=5)
                    await asyncio.wait_for(creator_finished.wait(), timeout=5)
                return job

            monkeypatch.setattr(persistence._common, "_owned_job", coordinated_owned_job)

            async def create_run() -> persistence.ParseRunResult:
                try:
                    return await persistence.begin_parse_run(
                        creator_session,
                        user_id,
                        import_job.job_id,
                        expected_job_version=1,
                        idempotency_key="shared-run-request-0001",
                    )
                finally:
                    creator_finished.set()

            async def replay_run() -> persistence.ParseRunResult:
                return await persistence.begin_parse_run(
                    retry_session,
                    user_id,
                    import_job.job_id,
                    expected_job_version=1,
                    idempotency_key="shared-run-request-0001",
                )

            created, replayed = await asyncio.gather(create_run(), replay_run())

        assert not created.replayed
        assert replayed.replayed
        assert replayed.run_id == created.run_id
        assert created.job_version == replayed.job_version == 2

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(database.dispose())


def test_concurrent_run_and_chunk_requests_replay_the_committed_winners(
    persistence_environment: tuple[TestClient, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, database_path = persistence_environment
    user_id, _ = _register(client, "concurrent-replay-user")
    events, parser_stats = _bookmark_events(tmp_path / "concurrent-bookmarks.html")
    database = Database(f"sqlite+aiosqlite:///{database_path.as_posix()}")

    async def scenario() -> None:
        async with database.sessions() as session:
            import_job = await persistence.create_import(
                session,
                user_id,
                source_sha256=parser_stats.source_sha256,
                source_size_bytes=parser_stats.source_size_bytes,
                original_filename="concurrent-bookmarks.html",
                idempotency_key="concurrent-upload-request-0001",
            )

        original_run_replay = persistence._common._parse_run_replay
        initial_run_lookups: set[int] = set()
        both_run_lookups_finished = asyncio.Event()

        async def synchronized_run_replay(
            session: object,
            scoped_user_id: str,
            job_id: str,
            key_hash: str,
        ) -> persistence.ParseRunResult | None:
            replay = await original_run_replay(  # type: ignore[arg-type]
                session,
                scoped_user_id,
                job_id,
                key_hash,
            )
            session_key = id(session)
            if session_key not in initial_run_lookups:
                initial_run_lookups.add(session_key)
                if len(initial_run_lookups) == 2:
                    both_run_lookups_finished.set()
                await asyncio.wait_for(both_run_lookups_finished.wait(), timeout=5)
            return replay

        monkeypatch.setattr(persistence._common, "_parse_run_replay", synchronized_run_replay)
        async with database.sessions() as first_session, database.sessions() as second_session:

            async def begin(session: object) -> persistence.ParseRunResult:
                return await persistence.begin_parse_run(  # type: ignore[arg-type]
                    session,
                    user_id,
                    import_job.job_id,
                    expected_job_version=1,
                    idempotency_key="concurrent-run-request-0001",
                )

            run_results = await asyncio.gather(begin(first_session), begin(second_session))

        monkeypatch.setattr(persistence._common, "_parse_run_replay", original_run_replay)
        assert {result.replayed for result in run_results} == {False, True}
        assert len({result.run_id for result in run_results}) == 1
        assert {result.job_version for result in run_results} == {2}
        run_id = run_results[0].run_id

        original_chunk_checkpoint = persistence._common._parse_chunk_checkpoint
        initial_chunk_lookups: set[int] = set()
        both_chunk_lookups_finished = asyncio.Event()

        async def synchronized_chunk_checkpoint(
            session: object,
            scoped_user_id: str,
            scoped_run_id: str,
            chunk_index: int,
        ) -> BookmarkImportCheckpoint | None:
            checkpoint = await original_chunk_checkpoint(  # type: ignore[arg-type]
                session,
                scoped_user_id,
                scoped_run_id,
                chunk_index,
            )
            session_key = id(session)
            if session_key not in initial_chunk_lookups:
                initial_chunk_lookups.add(session_key)
                if len(initial_chunk_lookups) == 2:
                    both_chunk_lookups_finished.set()
                await asyncio.wait_for(both_chunk_lookups_finished.wait(), timeout=5)
            return checkpoint

        monkeypatch.setattr(
            persistence._common,
            "_parse_chunk_checkpoint",
            synchronized_chunk_checkpoint,
        )
        async with database.sessions() as first_session, database.sessions() as second_session:

            async def append(session: object) -> persistence.StageChunkResult:
                return await persistence.append_parse_chunk(  # type: ignore[arg-type]
                    session,
                    user_id,
                    import_job.job_id,
                    run_id,
                    chunk_index=0,
                    events=events[:3],  # type: ignore[arg-type]
                )

            chunk_results = await asyncio.gather(append(first_session), append(second_session))

        assert {result.replayed for result in chunk_results} == {False, True}
        assert len({result.payload_hash for result in chunk_results}) == 1

        async with database.sessions() as session:
            folder_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(BookmarkStagingFolder)
                    .where(BookmarkStagingFolder.run_id == run_id)
                )
                or 0
            )
            occurrence_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(BookmarkStagingOccurrence)
                    .where(BookmarkStagingOccurrence.run_id == run_id)
                )
                or 0
            )
            checkpoint_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(BookmarkImportCheckpoint)
                    .where(BookmarkImportCheckpoint.run_id == run_id)
                )
                or 0
            )
        assert folder_count == 2
        assert occurrence_count == 1
        assert checkpoint_count == 1

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(database.dispose())


def test_finalize_rejects_candidate_identity_projection_drift(
    persistence_environment: tuple[TestClient, Path],
    tmp_path: Path,
) -> None:
    client, database_path = persistence_environment
    user_id, _ = _register(client, "projection-drift-user")
    events, parser_stats = _bookmark_events(tmp_path / "projection-drift-bookmarks.html")
    database = Database(f"sqlite+aiosqlite:///{database_path.as_posix()}")

    async def scenario() -> None:
        async with database.sessions() as session:
            import_job = await persistence.create_import(
                session,
                user_id,
                source_sha256=parser_stats.source_sha256,
                source_size_bytes=parser_stats.source_size_bytes,
                original_filename="projection-drift-bookmarks.html",
                idempotency_key="projection-drift-upload-0001",
            )
        async with database.sessions() as session:
            parse_run = await persistence.begin_parse_run(
                session,
                user_id,
                import_job.job_id,
                expected_job_version=1,
                idempotency_key="projection-drift-run-0001",
            )
        async with database.sessions() as session:
            await persistence.append_parse_chunk(
                session,
                user_id,
                import_job.job_id,
                parse_run.run_id,
                chunk_index=0,
                events=events[:3],  # type: ignore[arg-type]
            )

        async with database.sessions() as session:
            occurrence_id = await session.scalar(
                select(BookmarkStagingOccurrence.id).where(
                    BookmarkStagingOccurrence.user_id == user_id,
                    BookmarkStagingOccurrence.run_id == parse_run.run_id,
                )
            )
            original_candidate_id = await session.scalar(
                select(BookmarkStagingCandidate.id).where(
                    BookmarkStagingCandidate.user_id == user_id,
                    BookmarkStagingCandidate.run_id == parse_run.run_id,
                )
            )
            assert occurrence_id is not None
            assert original_candidate_id is not None
            await session.execute(
                delete(BookmarkStagingCandidate).where(
                    BookmarkStagingCandidate.user_id == user_id,
                    BookmarkStagingCandidate.run_id == parse_run.run_id,
                    BookmarkStagingCandidate.id == original_candidate_id,
                )
            )
            fake_identity = "https://fake.example/"
            fake_candidate = BookmarkStagingCandidate(
                user_id=user_id,
                run_id=parse_run.run_id,
                identity_url=fake_identity,
                identity_hash=persistence._sha256(fake_identity),
                display_title="Fake candidate",
                host="fake.example",
                fetch_policy="public_revalidation_required",
                has_sensitive_url=False,
                proposed_action="create",
                occurrence_count=1,
                first_source_sequence=2,
            )
            session.add(fake_candidate)
            await session.flush()
            session.add(
                BookmarkStagingCandidateOccurrence(
                    user_id=user_id,
                    run_id=parse_run.run_id,
                    candidate_id=fake_candidate.id,
                    occurrence_id=occurrence_id,
                )
            )
            await session.commit()

        completion = persistence.ParseCompletion(
            source_sha256=parser_stats.source_sha256,
            source_sequence_count=3,
            folder_count=2,
            occurrence_count=1,
        )
        async with database.sessions() as session:
            with pytest.raises(
                persistence.BookmarkPersistenceValidationError,
                match="identity 投影不一致",
            ):
                await persistence.finalize_parse_run(
                    session,
                    user_id,
                    import_job.job_id,
                    parse_run.run_id,
                    expected_job_version=2,
                    completion=completion,
                )

        async with database.sessions() as session:
            stored_run = await session.scalar(
                select(BookmarkImportRun).where(BookmarkImportRun.id == parse_run.run_id)
            )
            current_run_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(BookmarkImportCurrentRun)
                    .where(BookmarkImportCurrentRun.job_id == import_job.job_id)
                )
                or 0
            )
        assert stored_run is not None
        assert stored_run.state == "running"
        assert stored_run.completion_hash is None
        assert current_run_count == 0

        async with database.sessions() as session:
            await session.execute(
                update(BookmarkImportRun)
                .where(BookmarkImportRun.id == parse_run.run_id)
                .values(
                    state="finalizing",
                    completion_hash=persistence._parse_completion_hash(
                        parser_stats.source_sha256,
                        completion,
                    ),
                )
            )
            await session.commit()
        async with database.sessions() as session:
            with pytest.raises(
                persistence.BookmarkPersistenceValidationError,
                match="identity 投影不一致",
            ):
                await persistence.recover_finalizing_parse_run(
                    session,
                    user_id,
                    import_job.job_id,
                    parse_run.run_id,
                    expected_job_version=2,
                )

        async with database.sessions() as session:
            recovered_run = await session.scalar(
                select(BookmarkImportRun).where(BookmarkImportRun.id == parse_run.run_id)
            )
        assert recovered_run is not None
        assert recovered_run.state == "finalizing"
        assert recovered_run.completion_hash is not None

        async with database.sessions() as session:
            with pytest.raises(IntegrityError, match="immutable"):
                await session.execute(
                    update(BookmarkStagingOccurrence)
                    .where(BookmarkStagingOccurrence.run_id == parse_run.run_id)
                    .values(raw_title="late tamper")
                )
                await session.commit()
            await session.rollback()

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(database.dispose())


def test_finalize_parse_run_serializes_with_a_late_chunk(
    persistence_environment: tuple[TestClient, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, database_path = persistence_environment
    user_id, _ = _register(client, "race-user")
    events, parser_stats = _bookmark_events(tmp_path / "race-bookmarks.html")
    database = Database(f"sqlite+aiosqlite:///{database_path.as_posix()}")

    async def scenario() -> None:
        async with database.sessions() as session:
            import_job = await persistence.create_import(
                session,
                user_id,
                source_sha256=parser_stats.source_sha256,
                source_size_bytes=parser_stats.source_size_bytes,
                original_filename="race-bookmarks.html",
                idempotency_key="race-upload-request-0001",
            )
        async with database.sessions() as session:
            parse_run = await persistence.begin_parse_run(
                session,
                user_id,
                import_job.job_id,
                expected_job_version=1,
                idempotency_key="race-parse-request-0001",
            )
        async with database.sessions() as session:
            await persistence.append_parse_chunk(
                session,
                user_id,
                import_job.job_id,
                parse_run.run_id,
                chunk_index=0,
                events=events[:3],  # type: ignore[arg-type]
            )

        validation_finished = asyncio.Event()
        late_append_finished = asyncio.Event()
        original_validate = persistence.staging._validate_complete_staging

        async def pause_after_validation(
            session: object,
            scoped_user_id: str,
            run_id: str,
            completion: persistence.ParseCompletion,
        ) -> None:
            await original_validate(session, scoped_user_id, run_id, completion)  # type: ignore[arg-type]
            validation_finished.set()
            with suppress(TimeoutError):
                await asyncio.wait_for(late_append_finished.wait(), timeout=0.5)

        monkeypatch.setattr(
            persistence.staging,
            "_validate_complete_staging",
            pause_after_validation,
        )
        completion = persistence.ParseCompletion(
            source_sha256=parser_stats.source_sha256,
            source_sequence_count=3,
            folder_count=2,
            occurrence_count=1,
        )

        async def finalize() -> persistence.ParsePreviewSummary:
            async with database.sessions() as session:
                return await persistence.finalize_parse_run(
                    session,
                    user_id,
                    import_job.job_id,
                    parse_run.run_id,
                    expected_job_version=2,
                    completion=completion,
                )

        async def append_late() -> persistence.StageChunkResult:
            await asyncio.wait_for(validation_finished.wait(), timeout=5)
            try:
                async with database.sessions() as session:
                    return await persistence.append_parse_chunk(
                        session,
                        user_id,
                        import_job.job_id,
                        parse_run.run_id,
                        chunk_index=1,
                        events=events[3:],  # type: ignore[arg-type]
                    )
            finally:
                late_append_finished.set()

        final_result, append_result = await asyncio.gather(
            finalize(),
            append_late(),
            return_exceptions=True,
        )
        assert isinstance(final_result, persistence.ParsePreviewSummary)
        assert isinstance(append_result, persistence.BookmarkPersistenceConflictError)

        async with database.sessions() as session:
            stored_run = await session.scalar(
                select(BookmarkImportRun).where(BookmarkImportRun.id == parse_run.run_id)
            )
            folder_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(BookmarkStagingFolder)
                    .where(BookmarkStagingFolder.run_id == parse_run.run_id)
                )
                or 0
            )
            occurrence_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(BookmarkStagingOccurrence)
                    .where(BookmarkStagingOccurrence.run_id == parse_run.run_id)
                )
                or 0
            )
            checkpoint_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(BookmarkImportCheckpoint)
                    .where(
                        BookmarkImportCheckpoint.run_id == parse_run.run_id,
                        BookmarkImportCheckpoint.phase == "parse",
                    )
                )
                or 0
            )
        assert stored_run is not None
        assert stored_run.state == "complete"
        assert stored_run.source_sequence_count == 3
        assert stored_run.folder_count == folder_count == 2
        assert stored_run.occurrence_count == occurrence_count == 1
        assert checkpoint_count == 1

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(database.dispose())


def test_finalizing_parse_run_recovers_after_worker_restart(
    persistence_environment: tuple[TestClient, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, database_path = persistence_environment
    user_id, _ = _register(client, "finalizing-recovery-user")
    events, parser_stats = _bookmark_events(tmp_path / "recovery-bookmarks.html")
    database = Database(f"sqlite+aiosqlite:///{database_path.as_posix()}")

    async def scenario() -> persistence.ParsePreviewSummary:
        async with database.sessions() as session:
            import_job = await persistence.create_import(
                session,
                user_id,
                source_sha256=parser_stats.source_sha256,
                source_size_bytes=parser_stats.source_size_bytes,
                original_filename="recovery-bookmarks.html",
                idempotency_key="recovery-upload-request-0001",
            )
        async with database.sessions() as session:
            parse_run = await persistence.begin_parse_run(
                session,
                user_id,
                import_job.job_id,
                expected_job_version=1,
                idempotency_key="recovery-parse-request-0001",
            )
        async with database.sessions() as session:
            await persistence.append_parse_chunk(
                session,
                user_id,
                import_job.job_id,
                parse_run.run_id,
                chunk_index=0,
                events=events,  # type: ignore[arg-type]
            )

        completion = persistence.ParseCompletion(
            source_sha256=parser_stats.source_sha256,
            source_sequence_count=6,
            folder_count=2,
            occurrence_count=4,
        )
        original_validate = persistence.staging._validate_complete_staging

        async def interrupt_after_seal(*_: object, **__: object) -> None:
            raise asyncio.CancelledError

        monkeypatch.setattr(
            persistence.staging,
            "_validate_complete_staging",
            interrupt_after_seal,
        )
        async with database.sessions() as session:
            with pytest.raises(asyncio.CancelledError):
                await persistence.finalize_parse_run(
                    session,
                    user_id,
                    import_job.job_id,
                    parse_run.run_id,
                    expected_job_version=2,
                    completion=completion,
                )
        monkeypatch.setattr(
            persistence.staging,
            "_validate_complete_staging",
            original_validate,
        )

        async with database.sessions() as session:
            interrupted_run = await session.scalar(
                select(BookmarkImportRun).where(BookmarkImportRun.id == parse_run.run_id)
            )
        assert interrupted_run is not None
        assert interrupted_run.state == "finalizing"
        assert interrupted_run.completion_hash == persistence._parse_completion_hash(
            parser_stats.source_sha256,
            completion,
        )

        async with database.sessions() as session:
            with pytest.raises(IntegrityError, match="candidate structure is immutable"):
                await session.execute(
                    update(BookmarkStagingCandidate)
                    .where(
                        BookmarkStagingCandidate.user_id == user_id,
                        BookmarkStagingCandidate.run_id == parse_run.run_id,
                    )
                    .values(identity_url="https://tampered-during-finalizing.example/")
                )
                await session.commit()
            await session.rollback()

        async with database.sessions() as session:
            return await persistence.recover_finalizing_parse_run(
                session,
                user_id,
                import_job.job_id,
                parse_run.run_id,
                expected_job_version=2,
            )

    try:
        recovered = asyncio.run(scenario())
    finally:
        asyncio.run(database.dispose())

    assert recovered.job_version == 3
    assert recovered.preview_version == 1
    assert recovered.source_sequence_count == 6
    assert recovered.folder_count == 2
    assert recovered.occurrence_count == 4
    assert recovered.candidate_count == 2


def test_parse_run_rejects_algorithm_version_drift(
    persistence_environment: tuple[TestClient, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, database_path = persistence_environment
    user_id, _ = _register(client, "version-drift-user")
    events, parser_stats = _bookmark_events(tmp_path / "version-drift-bookmarks.html")
    database = Database(f"sqlite+aiosqlite:///{database_path.as_posix()}")

    async def scenario() -> None:
        async with database.sessions() as session:
            import_job = await persistence.create_import(
                session,
                user_id,
                source_sha256=parser_stats.source_sha256,
                source_size_bytes=parser_stats.source_size_bytes,
                original_filename="version-drift-bookmarks.html",
                idempotency_key="version-drift-upload-0001",
            )
        async with database.sessions() as session:
            parse_run = await persistence.begin_parse_run(
                session,
                user_id,
                import_job.job_id,
                expected_job_version=1,
                idempotency_key="version-drift-parse-request-0001",
            )

        active_normalizer_version = persistence._common.NORMALIZER_VERSION
        monkeypatch.setattr(persistence._common, "NORMALIZER_VERSION", "normalizer.future")
        async with database.sessions() as session:
            with pytest.raises(
                persistence.BookmarkPersistenceConflictError,
                match="版本不可用",
            ):
                await persistence.append_parse_chunk(
                    session,
                    user_id,
                    import_job.job_id,
                    parse_run.run_id,
                    chunk_index=0,
                    events=events,  # type: ignore[arg-type]
                )

        monkeypatch.setattr(persistence._common, "NORMALIZER_VERSION", active_normalizer_version)
        async with database.sessions() as session:
            await persistence.append_parse_chunk(
                session,
                user_id,
                import_job.job_id,
                parse_run.run_id,
                chunk_index=0,
                events=events,  # type: ignore[arg-type]
            )
        completion = persistence.ParseCompletion(
            source_sha256=parser_stats.source_sha256,
            source_sequence_count=6,
            folder_count=2,
            occurrence_count=4,
        )
        async with database.sessions() as session:
            published = await persistence.finalize_parse_run(
                session,
                user_id,
                import_job.job_id,
                parse_run.run_id,
                expected_job_version=2,
                completion=completion,
            )

        monkeypatch.setattr(persistence._common, "NORMALIZER_VERSION", "normalizer.future")
        async with database.sessions() as session:
            replayed_preview = await persistence.finalize_parse_run(
                session,
                user_id,
                import_job.job_id,
                parse_run.run_id,
                expected_job_version=2,
                completion=completion,
            )
        async with database.sessions() as session:
            replayed_chunk = await persistence.append_parse_chunk(
                session,
                user_id,
                import_job.job_id,
                parse_run.run_id,
                chunk_index=0,
                events=events,  # type: ignore[arg-type]
            )

        assert replayed_preview == published
        assert replayed_chunk.replayed

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(database.dispose())


def test_empty_bookmark_export_can_publish_zero_event_preview(
    persistence_environment: tuple[TestClient, Path],
    tmp_path: Path,
) -> None:
    client, database_path = persistence_environment
    user_id, _ = _register(client, "empty-bookmarks-user")
    source_path = tmp_path / "empty-bookmarks.html"
    source_path.write_text(
        "<!DOCTYPE NETSCAPE-Bookmark-file-1>\n<DL><p>\n</DL><p>\n",
        encoding="utf-8",
    )
    parser_stats = ParserStats()
    assert list(iter_netscape_events(source_path, stats=parser_stats)) == []
    assert parser_stats.source_sha256
    database = Database(f"sqlite+aiosqlite:///{database_path.as_posix()}")

    async def scenario() -> persistence.ParsePreviewSummary:
        async with database.sessions() as session:
            import_job = await persistence.create_import(
                session,
                user_id,
                source_sha256=parser_stats.source_sha256,
                source_size_bytes=parser_stats.source_size_bytes,
                original_filename="empty-bookmarks.html",
                idempotency_key="empty-upload-request-0001",
            )
        async with database.sessions() as session:
            parse_run = await persistence.begin_parse_run(
                session,
                user_id,
                import_job.job_id,
                expected_job_version=1,
                idempotency_key="empty-parse-request-0001",
            )
        completion = persistence.ParseCompletion(
            source_sha256=parser_stats.source_sha256,
            source_sequence_count=0,
            folder_count=0,
            occurrence_count=0,
        )
        async with database.sessions() as session:
            with pytest.raises(
                persistence.BookmarkPersistenceValidationError,
                match="缺少完成检查点",
            ):
                await persistence.finalize_parse_run(
                    session,
                    user_id,
                    import_job.job_id,
                    parse_run.run_id,
                    expected_job_version=2,
                    completion=completion,
                )
        async with database.sessions() as session:
            empty_checkpoint = await persistence.append_parse_chunk(
                session,
                user_id,
                import_job.job_id,
                parse_run.run_id,
                chunk_index=0,
                events=[],
            )
        assert empty_checkpoint.processed_count == 0
        assert empty_checkpoint.source_sequence_start == 0
        assert empty_checkpoint.source_sequence_end == 0
        async with database.sessions() as session:
            return await persistence.finalize_parse_run(
                session,
                user_id,
                import_job.job_id,
                parse_run.run_id,
                expected_job_version=2,
                completion=completion,
            )

    try:
        preview = asyncio.run(scenario())
    finally:
        asyncio.run(database.dispose())

    assert preview.job_version == 3
    assert preview.preview_version == 1
    assert preview.candidate_count == 0
