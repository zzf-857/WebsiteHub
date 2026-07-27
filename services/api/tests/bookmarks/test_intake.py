import asyncio
import hashlib
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from sqlalchemy import func, select

from webhub.bookmarks import intake, persistence
from webhub.bookmarks.uploads import StagedBookmarkUpload, stage_bookmark_upload
from webhub.db.database import Database
from webhub.db.migrations import upgrade_database
from webhub.db.models import BookmarkImportJob, BookmarkImportSnapshot, User


def _export(label: str) -> bytes:
    return (
        "<!DOCTYPE NETSCAPE-Bookmark-file-1>\n"
        '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">\n'
        f'<DL><p><DT><A HREF="https://example.com/{label}">{label}</A></DL><p>\n'
    ).encode()


async def _chunks(payload: bytes) -> AsyncIterator[bytes]:
    midpoint = len(payload) // 2
    yield payload[:midpoint]
    await asyncio.sleep(0)
    yield payload[midpoint:]


async def _stage(
    data_directory: Path,
    account_id: str,
    payload: bytes,
) -> StagedBookmarkUpload:
    return await stage_bookmark_upload(
        _chunks(payload),
        data_directory=data_directory,
        account_id=account_id,
        original_filename="../../private\\bookmarks.html",
    )


@pytest.fixture
def intake_environment(tmp_path: Path) -> Iterator[tuple[Database, Path, str, str]]:
    database_path = tmp_path / "main.sqlite3"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    upgrade_database(database_url)
    database = Database(database_url)

    async def seed_users() -> tuple[str, str]:
        async with database.sessions() as session:
            alice = User(
                username="alice-intake",
                display_name="Alice",
                password_hash="not-used-by-intake-tests",
            )
            bob = User(
                username="bob-intake",
                display_name="Bob",
                password_hash="not-used-by-intake-tests",
            )
            session.add_all([alice, bob])
            await session.commit()
            return alice.id, bob.id

    alice_id, bob_id = asyncio.run(seed_users())
    yield database, tmp_path, alice_id, bob_id
    asyncio.run(database.dispose())


def _incoming_files(data_directory: Path) -> list[Path]:
    incoming = data_directory / "bookmark-imports" / "incoming"
    return [path for path in incoming.rglob("*") if path.is_file()] if incoming.exists() else []


def test_intake_publishes_verified_file_then_replays_without_overwrite(
    intake_environment: tuple[Database, Path, str, str],
) -> None:
    database, data_directory, alice_id, _ = intake_environment
    payload = _export("first")

    async def scenario() -> None:
        staged = await _stage(data_directory, alice_id, payload)
        async with database.sessions() as session:
            created = await intake.intake_bookmark_upload(
                session,
                data_directory=data_directory,
                account_id=alice_id,
                staged_upload=staged,
                idempotency_key="intake-happy-request-0001",
            )

        assert created.state == "queued_parse"
        assert created.job_version == 2
        assert not created.replayed
        assert not staged.temporary_path.exists()
        expected_path = (
            data_directory / "bookmark-imports" / alice_id / created.snapshot_id / "source.html"
        ).resolve()
        assert (data_directory / created.storage_key).resolve() == expected_path
        assert expected_path.read_bytes() == payload
        assert expected_path.is_relative_to(
            (data_directory / "bookmark-imports" / alice_id / created.snapshot_id).resolve()
        )

        async with database.sessions() as session:
            snapshot = await session.get(BookmarkImportSnapshot, created.snapshot_id)
        assert snapshot is not None
        assert snapshot.source_sha256 == hashlib.sha256(payload).hexdigest()
        assert snapshot.detected_encoding == "utf-8"
        assert snapshot.original_filename == "bookmarks.html"

        before = expected_path.stat()
        replay_staged = await _stage(data_directory, alice_id, payload)
        async with database.sessions() as session:
            replayed = await intake.intake_bookmark_upload(
                session,
                data_directory=data_directory,
                account_id=alice_id,
                staged_upload=replay_staged,
                idempotency_key="intake-happy-request-0001",
            )
        after = expected_path.stat()

        assert replayed.replayed
        assert replayed.snapshot_id == created.snapshot_id
        assert replayed.job_id == created.job_id
        assert replayed.job_version == 2
        assert after.st_ino == before.st_ino
        assert after.st_mtime_ns == before.st_mtime_ns
        assert not replay_staged.temporary_path.exists()
        assert _incoming_files(data_directory) == []

    asyncio.run(scenario())


def test_replay_releases_session_transaction_before_publish_hash(
    intake_environment: tuple[Database, Path, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, data_directory, alice_id, _ = intake_environment
    payload = _export("transaction-boundary")

    async def scenario() -> None:
        staged = await _stage(data_directory, alice_id, payload)
        async with database.sessions() as session:
            created = await intake.intake_bookmark_upload(
                session,
                data_directory=data_directory,
                account_id=alice_id,
                staged_upload=staged,
                idempotency_key="intake-transaction-boundary-0001",
            )

        destination = (data_directory / created.storage_key).resolve()
        before = destination.stat()
        replay_staged = await _stage(data_directory, alice_id, payload)
        transaction_states: list[bool] = []
        replay_session = None
        original_file_facts = intake._file_facts

        async def observe_file_hash(path: Path) -> tuple[str, int]:
            assert replay_session is not None
            transaction_states.append(replay_session.in_transaction())
            return await original_file_facts(path)

        monkeypatch.setattr(intake, "_file_facts", observe_file_hash)
        async with database.sessions() as session:
            replay_session = session
            replayed = await intake.intake_bookmark_upload(
                session,
                data_directory=data_directory,
                account_id=alice_id,
                staged_upload=replay_staged,
                idempotency_key="intake-transaction-boundary-0001",
            )
            assert not session.in_transaction()

        after = destination.stat()
        assert transaction_states == [False]
        assert replayed.replayed
        assert replayed.snapshot_id == created.snapshot_id
        assert replayed.job_id == created.job_id
        assert replayed.state == "queued_parse"
        assert replayed.job_version == 2
        assert destination.read_bytes() == payload
        assert after.st_ino == before.st_ino
        assert after.st_mtime_ns == before.st_mtime_ns
        assert not replay_staged.temporary_path.exists()
        assert _incoming_files(data_directory) == []

        async with database.sessions() as session:
            snapshot_count = await session.scalar(
                select(func.count())
                .select_from(BookmarkImportSnapshot)
                .where(BookmarkImportSnapshot.user_id == alice_id)
            )
            job = await session.scalar(
                select(BookmarkImportJob).where(BookmarkImportJob.user_id == alice_id)
            )
        assert snapshot_count == 1
        assert job is not None
        assert job.id == created.job_id
        assert job.state == "queued_parse"
        assert job.version == 2

    asyncio.run(scenario())


def test_retry_recovers_database_commit_before_file_publish(
    intake_environment: tuple[Database, Path, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, data_directory, alice_id, _ = intake_environment
    payload = _export("before-publish")
    original_publish = intake.publish_staged_upload
    should_crash = True

    async def crash_once(*args: object, **kwargs: object) -> intake.PublishedBookmarkFile:
        nonlocal should_crash
        if should_crash:
            should_crash = False
            raise RuntimeError("simulated crash before publish")
        return await original_publish(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(intake, "publish_staged_upload", crash_once)

    async def scenario() -> None:
        first_staged = await _stage(data_directory, alice_id, payload)
        async with database.sessions() as session:
            with pytest.raises(RuntimeError, match="before publish"):
                await intake.intake_bookmark_upload(
                    session,
                    data_directory=data_directory,
                    account_id=alice_id,
                    staged_upload=first_staged,
                    idempotency_key="intake-before-publish-0001",
                )
        assert not first_staged.temporary_path.exists()

        async with database.sessions() as session:
            snapshots = (
                await session.scalars(
                    select(BookmarkImportSnapshot).where(BookmarkImportSnapshot.user_id == alice_id)
                )
            ).all()
            jobs = (
                await session.scalars(
                    select(BookmarkImportJob).where(BookmarkImportJob.user_id == alice_id)
                )
            ).all()
        assert len(snapshots) == len(jobs) == 1
        assert jobs[0].state == "receiving"

        retry_staged = await _stage(data_directory, alice_id, payload)
        async with database.sessions() as session:
            recovered = await intake.intake_bookmark_upload(
                session,
                data_directory=data_directory,
                account_id=alice_id,
                staged_upload=retry_staged,
                idempotency_key="intake-before-publish-0001",
            )
        assert recovered.replayed
        assert recovered.snapshot_id == snapshots[0].id
        assert recovered.job_id == jobs[0].id
        assert recovered.state == "queued_parse"
        assert (data_directory / recovered.storage_key).read_bytes() == payload
        assert _incoming_files(data_directory) == []

    asyncio.run(scenario())


def test_retry_recovers_file_publish_before_queued_cas(
    intake_environment: tuple[Database, Path, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, data_directory, alice_id, _ = intake_environment
    payload = _export("after-publish")
    original_queue = persistence.queue_import_for_parse
    should_crash = True

    async def crash_once(*args: object, **kwargs: object) -> persistence.ImportJobResult:
        nonlocal should_crash
        if should_crash:
            should_crash = False
            raise RuntimeError("simulated crash after publish")
        return await original_queue(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(persistence, "queue_import_for_parse", crash_once)

    async def scenario() -> None:
        first_staged = await _stage(data_directory, alice_id, payload)
        async with database.sessions() as session:
            with pytest.raises(RuntimeError, match="after publish"):
                await intake.intake_bookmark_upload(
                    session,
                    data_directory=data_directory,
                    account_id=alice_id,
                    staged_upload=first_staged,
                    idempotency_key="intake-after-publish-0001",
                )

        async with database.sessions() as session:
            snapshot = await session.scalar(
                select(BookmarkImportSnapshot).where(BookmarkImportSnapshot.user_id == alice_id)
            )
            job = await session.scalar(
                select(BookmarkImportJob).where(BookmarkImportJob.user_id == alice_id)
            )
        assert snapshot is not None and job is not None
        assert job.state == "receiving"
        final_path = (data_directory / snapshot.storage_key).resolve()
        assert final_path.read_bytes() == payload
        before = final_path.stat()
        assert not first_staged.temporary_path.exists()

        retry_staged = await _stage(data_directory, alice_id, payload)
        async with database.sessions() as session:
            recovered = await intake.intake_bookmark_upload(
                session,
                data_directory=data_directory,
                account_id=alice_id,
                staged_upload=retry_staged,
                idempotency_key="intake-after-publish-0001",
            )
        after = final_path.stat()

        assert recovered.replayed
        assert recovered.snapshot_id == snapshot.id
        assert recovered.job_id == job.id
        assert recovered.state == "queued_parse"
        assert after.st_ino == before.st_ino
        assert after.st_mtime_ns == before.st_mtime_ns
        assert not retry_staged.temporary_path.exists()

    asyncio.run(scenario())


def test_concurrent_same_key_and_content_returns_one_snapshot_and_job(
    intake_environment: tuple[Database, Path, str, str],
) -> None:
    database, data_directory, alice_id, _ = intake_environment
    payload = _export("concurrent-same")

    async def scenario() -> None:
        first_staged, second_staged = await asyncio.gather(
            _stage(data_directory, alice_id, payload),
            _stage(data_directory, alice_id, payload),
        )
        async with database.sessions() as first_session, database.sessions() as second_session:
            first, second = await asyncio.gather(
                intake.intake_bookmark_upload(
                    first_session,
                    data_directory=data_directory,
                    account_id=alice_id,
                    staged_upload=first_staged,
                    idempotency_key="intake-concurrent-same-0001",
                ),
                intake.intake_bookmark_upload(
                    second_session,
                    data_directory=data_directory,
                    account_id=alice_id,
                    staged_upload=second_staged,
                    idempotency_key="intake-concurrent-same-0001",
                ),
            )

        assert first.snapshot_id == second.snapshot_id
        assert first.job_id == second.job_id
        assert first.state == second.state == "queued_parse"
        assert first.job_version == second.job_version == 2
        assert first.replayed or second.replayed
        assert (data_directory / first.storage_key).read_bytes() == payload
        assert not first_staged.temporary_path.exists()
        assert not second_staged.temporary_path.exists()

        async with database.sessions() as session:
            snapshot_count = await session.scalar(
                select(func.count())
                .select_from(BookmarkImportSnapshot)
                .where(BookmarkImportSnapshot.user_id == alice_id)
            )
            job_count = await session.scalar(
                select(func.count())
                .select_from(BookmarkImportJob)
                .where(BookmarkImportJob.user_id == alice_id)
            )
        assert snapshot_count == job_count == 1

    asyncio.run(scenario())


def test_concurrent_same_key_with_different_content_conflicts_and_never_overwrites(
    intake_environment: tuple[Database, Path, str, str],
) -> None:
    database, data_directory, alice_id, _ = intake_environment
    first_payload = _export("content-a")
    second_payload = _export("content-b")

    async def scenario() -> None:
        first_staged, second_staged = await asyncio.gather(
            _stage(data_directory, alice_id, first_payload),
            _stage(data_directory, alice_id, second_payload),
        )
        async with database.sessions() as first_session, database.sessions() as second_session:
            outcomes = await asyncio.gather(
                intake.intake_bookmark_upload(
                    first_session,
                    data_directory=data_directory,
                    account_id=alice_id,
                    staged_upload=first_staged,
                    idempotency_key="intake-concurrent-conflict-0001",
                ),
                intake.intake_bookmark_upload(
                    second_session,
                    data_directory=data_directory,
                    account_id=alice_id,
                    staged_upload=second_staged,
                    idempotency_key="intake-concurrent-conflict-0001",
                ),
                return_exceptions=True,
            )

        successes = [
            outcome for outcome in outcomes if isinstance(outcome, persistence.ImportJobResult)
        ]
        conflicts = [
            outcome
            for outcome in outcomes
            if isinstance(outcome, persistence.BookmarkPersistenceConflictError)
        ]
        assert len(successes) == len(conflicts) == 1
        assert conflicts[0].status_code == 409
        assert not first_staged.temporary_path.exists()
        assert not second_staged.temporary_path.exists()

        success = successes[0]
        final_payload = (data_directory / success.storage_key).read_bytes()
        assert final_payload in {first_payload, second_payload}
        async with database.sessions() as session:
            snapshot = await session.get(BookmarkImportSnapshot, success.snapshot_id)
        assert snapshot is not None
        assert hashlib.sha256(final_payload).hexdigest() == snapshot.source_sha256

    asyncio.run(scenario())


def test_staged_upload_cannot_be_claimed_by_another_account(
    intake_environment: tuple[Database, Path, str, str],
) -> None:
    database, data_directory, alice_id, bob_id = intake_environment
    payload = _export("account-isolation")

    async def scenario() -> None:
        staged = await _stage(data_directory, alice_id, payload)
        async with database.sessions() as session:
            with pytest.raises(
                persistence.BookmarkPersistenceValidationError,
                match="存储目录",
            ):
                await intake.intake_bookmark_upload(
                    session,
                    data_directory=data_directory,
                    account_id=bob_id,
                    staged_upload=staged,
                    idempotency_key="intake-cross-account-0001",
                )
        assert staged.temporary_path.exists()

        async with database.sessions() as session:
            bob_snapshots = await session.scalar(
                select(func.count())
                .select_from(BookmarkImportSnapshot)
                .where(BookmarkImportSnapshot.user_id == bob_id)
            )
        assert bob_snapshots == 0

        async with database.sessions() as session:
            accepted = await intake.intake_bookmark_upload(
                session,
                data_directory=data_directory,
                account_id=alice_id,
                staged_upload=staged,
                idempotency_key="intake-cross-account-0001",
            )
        assert accepted.state == "queued_parse"
        assert (data_directory / accepted.storage_key).read_bytes() == payload

    asyncio.run(scenario())


@pytest.mark.parametrize("job_state", ["receiving", "queued_parse"])
def test_replay_rejects_snapshot_directory_redirect_without_reading_target(
    intake_environment: tuple[Database, Path, str, str],
    monkeypatch: pytest.MonkeyPatch,
    job_state: str,
) -> None:
    database, data_directory, alice_id, _ = intake_environment
    payload = _export(f"redirect-{job_state}")
    idempotency_key = f"intake-redirect-{job_state}-0001"

    async def create_published_import() -> persistence.ImportJobResult:
        staged = await _stage(data_directory, alice_id, payload)
        if job_state == "queued_parse":
            async with database.sessions() as session:
                return await intake.intake_bookmark_upload(
                    session,
                    data_directory=data_directory,
                    account_id=alice_id,
                    staged_upload=staged,
                    idempotency_key=idempotency_key,
                )

        async with database.sessions() as session:
            receiving = await persistence.create_import(
                session,
                alice_id,
                source_sha256=staged.source_sha256,
                source_size_bytes=staged.source_size_bytes,
                original_filename=staged.display_filename,
                idempotency_key=idempotency_key,
                detected_encoding=staged.encoding,
                ready_for_parse=False,
            )
        await intake.publish_staged_upload(
            staged,
            data_directory=data_directory,
            account_id=alice_id,
            snapshot_id=receiving.snapshot_id,
            storage_key=receiving.storage_key,
            allow_create=True,
        )
        return receiving

    import_job = asyncio.run(create_published_import())
    snapshot_directory = data_directory / "bookmark-imports" / alice_id / import_job.snapshot_id
    saved_directory = snapshot_directory.with_name(f"{import_job.snapshot_id}-saved")
    snapshot_directory.rename(saved_directory)
    redirected_directory = data_directory / "bookmark-imports" / "other-account" / "other-snapshot"
    redirected_directory.mkdir(parents=True)
    redirected_source = redirected_directory / "source.html"
    redirected_source.write_bytes(b"must-not-be-read-or-overwritten")

    try:
        snapshot_directory.symlink_to(redirected_directory, target_is_directory=True)
    except OSError:
        saved_directory.rename(snapshot_directory)
        original_link_check = intake._is_link_like_directory
        monkeypatch.setattr(
            intake,
            "_is_link_like_directory",
            lambda path: path == snapshot_directory or original_link_check(path),
        )

    reads: list[Path] = []
    original_file_facts = intake._file_facts

    async def observe_file_read(path: Path) -> tuple[str, int]:
        reads.append(path)
        return await original_file_facts(path)

    monkeypatch.setattr(intake, "_file_facts", observe_file_read)

    async def replay() -> None:
        staged = await _stage(data_directory, alice_id, payload)
        async with database.sessions() as session:
            with pytest.raises(persistence.BookmarkPersistenceConflictError) as captured:
                await intake.intake_bookmark_upload(
                    session,
                    data_directory=data_directory,
                    account_id=alice_id,
                    staged_upload=staged,
                    idempotency_key=idempotency_key,
                )
        assert captured.value.status_code == 409
        assert not staged.temporary_path.exists()

    asyncio.run(replay())
    assert reads == []
    assert redirected_source.read_bytes() == b"must-not-be-read-or-overwritten"


def test_queue_import_rejects_boolean_expected_version(
    intake_environment: tuple[Database, Path, str, str],
) -> None:
    database, _, alice_id, _ = intake_environment

    async def scenario() -> None:
        async with database.sessions() as session:
            receiving = await persistence.create_import(
                session,
                alice_id,
                source_sha256="f" * 64,
                source_size_bytes=128,
                original_filename="bookmarks.html",
                idempotency_key="intake-boolean-version-0001",
                ready_for_parse=False,
            )
        async with database.sessions() as session:
            with pytest.raises(
                persistence.BookmarkPersistenceValidationError,
                match="正整数",
            ):
                await persistence.queue_import_for_parse(
                    session,
                    alice_id,
                    receiving.job_id,
                    expected_job_version=True,
                )

    asyncio.run(scenario())
