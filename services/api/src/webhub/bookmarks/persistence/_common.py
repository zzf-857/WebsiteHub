from __future__ import annotations

import errno
import hashlib
import json
import re
import sqlite3
import unicodedata
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from uuid import UUID, uuid5

from sqlalchemy import and_, select
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.bookmarks.models import (
    BookmarkEvent,
    ParsedFolder,
)
from webhub.bookmarks.normalization import NORMALIZER_VERSION
from webhub.bookmarks.parser import PARSER_VERSION
from webhub.db.models import (
    BookmarkImportCheckpoint,
    BookmarkImportJob,
    BookmarkImportRun,
    BookmarkImportSnapshot,
)

SKILL_VERSION = "import-browser-bookmarks.v2"
MAX_STAGE_EVENTS = 1_000
_SHA256 = re.compile(r"[0-9a-f]{64}")
_STABLE_ID_NAMESPACE = UUID("9922dafe-5a9c-44c0-a0a3-a184d0419072")


class BookmarkPersistenceError(Exception):
    status_code = 400

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class BookmarkPersistenceNotFoundError(BookmarkPersistenceError):
    status_code = 404


class BookmarkPersistenceConflictError(BookmarkPersistenceError):
    status_code = 409


class BookmarkPersistenceValidationError(BookmarkPersistenceError):
    status_code = 422


@dataclass(frozen=True, slots=True)
class ImportJobResult:
    snapshot_id: str
    job_id: str
    storage_key: str
    state: str
    job_version: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class SameSourceResult:
    snapshot_id: str
    job_id: str
    state: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ParseRunResult:
    run_id: str
    attempt_number: int
    job_version: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class StageChunkResult:
    run_id: str
    chunk_index: int
    source_sequence_start: int
    source_sequence_end: int
    processed_count: int
    payload_hash: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class ParseCompletion:
    source_sha256: str
    source_sequence_count: int
    folder_count: int
    occurrence_count: int


@dataclass(frozen=True, slots=True)
class ParsePreviewSummary:
    job_id: str
    run_id: str
    job_version: int
    preview_version: int
    source_sequence_count: int
    folder_count: int
    occurrence_count: int
    candidate_count: int


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _validate_digest(value: str, *, field: str) -> str:
    normalized = value.strip().casefold()
    if not _SHA256.fullmatch(normalized):
        raise BookmarkPersistenceValidationError(f"{field} 必须是 64 位 SHA-256")
    return normalized


def _key_hash(value: str, *, field: str) -> str:
    candidate = value.strip()
    if not 16 <= len(candidate) <= 512:
        raise BookmarkPersistenceValidationError(f"{field}长度必须在 16 到 512 之间")
    return _sha256(candidate)


def _display_filename(value: str | None) -> str | None:
    if value is None:
        return None
    leaf = value.replace("\\", "/").rsplit("/", 1)[-1]
    display = " ".join(unicodedata.normalize("NFKC", leaf).split())
    display = "".join(character for character in display if ord(character) >= 32)
    return display[:255] or None


def _stable_id(run_id: str, kind: str, source_key: str) -> str:
    return str(uuid5(_STABLE_ID_NAMESPACE, f"{run_id}:{kind}:{source_key}"))


def _event_payload(event: BookmarkEvent) -> dict[str, object]:
    payload = asdict(event)
    payload["kind"] = "folder" if isinstance(event, ParsedFolder) else "bookmark"
    return payload


def _event_batch_hash(events: Sequence[BookmarkEvent]) -> str:
    payload = json.dumps(
        [_event_payload(event) for event in events],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _sha256(payload)


def _job_result(
    snapshot: BookmarkImportSnapshot,
    job: BookmarkImportJob,
    *,
    replayed: bool,
) -> ImportJobResult:
    return ImportJobResult(
        snapshot_id=snapshot.id,
        job_id=job.id,
        storage_key=snapshot.storage_key,
        state=job.state,
        job_version=job.version,
        replayed=replayed,
    )


async def _owned_job(session: AsyncSession, user_id: str, job_id: str) -> BookmarkImportJob:
    job = await session.scalar(
        select(BookmarkImportJob).where(
            BookmarkImportJob.user_id == user_id,
            BookmarkImportJob.id == job_id,
        )
    )
    if job is None:
        raise BookmarkPersistenceNotFoundError("书签导入任务不存在")
    return job


async def _owned_import(
    session: AsyncSession,
    user_id: str,
    job_id: str,
) -> tuple[BookmarkImportSnapshot, BookmarkImportJob]:
    row = (
        await session.execute(
            select(BookmarkImportSnapshot, BookmarkImportJob)
            .join(
                BookmarkImportJob,
                and_(
                    BookmarkImportJob.user_id == BookmarkImportSnapshot.user_id,
                    BookmarkImportJob.snapshot_id == BookmarkImportSnapshot.id,
                ),
            )
            .where(
                BookmarkImportJob.user_id == user_id,
                BookmarkImportJob.id == job_id,
            )
        )
    ).one_or_none()
    if row is None:
        raise BookmarkPersistenceNotFoundError("书签导入任务不存在")
    snapshot, job = row
    return snapshot, job


async def _owned_run(
    session: AsyncSession,
    user_id: str,
    job_id: str,
    run_id: str,
) -> BookmarkImportRun:
    run = await session.scalar(
        select(BookmarkImportRun).where(
            BookmarkImportRun.user_id == user_id,
            BookmarkImportRun.job_id == job_id,
            BookmarkImportRun.id == run_id,
        )
    )
    if run is None:
        raise BookmarkPersistenceNotFoundError("书签解析运行不存在")
    return run


async def _parse_run_replay(
    session: AsyncSession,
    user_id: str,
    job_id: str,
    key_hash: str,
) -> ParseRunResult | None:
    row = (
        await session.execute(
            select(BookmarkImportRun, BookmarkImportJob.version)
            .join(
                BookmarkImportJob,
                and_(
                    BookmarkImportJob.user_id == BookmarkImportRun.user_id,
                    BookmarkImportJob.id == BookmarkImportRun.job_id,
                ),
            )
            .where(
                BookmarkImportRun.user_id == user_id,
                BookmarkImportRun.job_id == job_id,
                BookmarkImportRun.run_idempotency_key_hash == key_hash,
            )
        )
    ).one_or_none()
    if row is None:
        return None
    run, job_version = row
    return ParseRunResult(
        run_id=run.id,
        attempt_number=run.attempt_number,
        job_version=int(job_version),
        replayed=True,
    )


async def _parse_chunk_checkpoint(
    session: AsyncSession,
    user_id: str,
    run_id: str,
    chunk_index: int,
) -> BookmarkImportCheckpoint | None:
    return await session.scalar(
        select(BookmarkImportCheckpoint).where(
            BookmarkImportCheckpoint.user_id == user_id,
            BookmarkImportCheckpoint.run_id == run_id,
            BookmarkImportCheckpoint.phase == "parse",
            BookmarkImportCheckpoint.chunk_index == chunk_index,
        )
    )


def _parse_chunk_replay(
    checkpoint: BookmarkImportCheckpoint,
    *,
    payload_hash: str,
) -> StageChunkResult:
    if checkpoint.input_hash != payload_hash:
        raise BookmarkPersistenceConflictError("解析分片序号已绑定不同内容")
    if checkpoint.state != "complete":
        raise BookmarkPersistenceConflictError("解析分片仍处于未完成状态")
    return StageChunkResult(
        run_id=checkpoint.run_id,
        chunk_index=checkpoint.chunk_index,
        source_sequence_start=checkpoint.source_sequence_start or 0,
        source_sequence_end=checkpoint.source_sequence_end or 0,
        processed_count=checkpoint.processed_count,
        payload_hash=payload_hash,
        replayed=True,
    )


def _parse_completion_hash(source_hash: str, completion: ParseCompletion) -> str:
    payload = json.dumps(
        {
            "source_sha256": source_hash,
            "source_sequence_count": completion.source_sequence_count,
            "folder_count": completion.folder_count,
            "occurrence_count": completion.occurrence_count,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return _sha256(payload)


def _is_database_busy(error: OperationalError) -> bool:
    return "locked" in str(error.orig).casefold()


def is_database_storage_exhausted(error: DBAPIError) -> bool:
    original = error.orig
    sqlite_error_code = getattr(original, "sqlite_errorcode", None)
    if isinstance(sqlite_error_code, int):
        return sqlite_error_code & 0xFF == sqlite3.SQLITE_FULL
    return isinstance(original, OSError) and original.errno in {
        errno.ENOSPC,
        getattr(errno, "EDQUOT", errno.ENOSPC),
    }


def _assert_run_versions(run: BookmarkImportRun) -> None:
    if run.parser_version != PARSER_VERSION or run.normalizer_version != NORMALIZER_VERSION:
        raise BookmarkPersistenceConflictError(
            "解析运行依赖的 parser/normalizer 版本不可用，请重新开始解析"
        )
