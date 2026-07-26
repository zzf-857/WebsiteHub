from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from uuid import UUID, uuid5

from sqlalchemy import and_, case, delete, func, literal, select, union_all, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.bookmarks.models import (
    BookmarkEvent,
    NormalizationStatus,
    ParsedFolder,
    ParserLimits,
)
from webhub.bookmarks.normalization import NORMALIZER_VERSION, normalize_bookmark_url
from webhub.bookmarks.parser import PARSER_VERSION
from webhub.bookmarks.privacy import sensitive_url_keys
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
    new_id,
    utc_now,
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


def _assert_run_versions(run: BookmarkImportRun) -> None:
    if run.parser_version != PARSER_VERSION or run.normalizer_version != NORMALIZER_VERSION:
        raise BookmarkPersistenceConflictError(
            "解析运行依赖的 parser/normalizer 版本不可用，请重新开始解析"
        )


async def _completed_parse_preview_replay(
    session: AsyncSession,
    user_id: str,
    job_id: str,
    run_id: str,
    *,
    expected_job_version: int,
    completion_hash: str,
) -> ParsePreviewSummary | None:
    row = (
        await session.execute(
            select(BookmarkImportJob, BookmarkImportRun)
            .select_from(BookmarkImportCurrentRun)
            .join(
                BookmarkImportJob,
                and_(
                    BookmarkImportJob.user_id == BookmarkImportCurrentRun.user_id,
                    BookmarkImportJob.id == BookmarkImportCurrentRun.job_id,
                ),
            )
            .join(
                BookmarkImportRun,
                and_(
                    BookmarkImportRun.user_id == BookmarkImportCurrentRun.user_id,
                    BookmarkImportRun.job_id == BookmarkImportCurrentRun.job_id,
                    BookmarkImportRun.id == BookmarkImportCurrentRun.run_id,
                ),
            )
            .where(
                BookmarkImportCurrentRun.user_id == user_id,
                BookmarkImportCurrentRun.job_id == job_id,
                BookmarkImportCurrentRun.run_id == run_id,
            )
        )
    ).one_or_none()
    if row is None:
        return None
    job, run = row
    if (
        job.state != "parse_preview_ready"
        or job.version != expected_job_version + 1
        or run.state != "complete"
        or run.completion_hash != completion_hash
    ):
        return None
    return ParsePreviewSummary(
        job_id=job.id,
        run_id=run.id,
        job_version=job.version,
        preview_version=job.preview_version,
        source_sequence_count=run.source_sequence_count,
        folder_count=run.folder_count,
        occurrence_count=run.occurrence_count,
        candidate_count=run.candidate_count,
    )


async def _release_parse_run_seal(
    session: AsyncSession,
    user_id: str,
    job_id: str,
    run_id: str,
    completion_hash: str,
) -> None:
    try:
        await session.execute(
            update(BookmarkImportRun)
            .where(
                BookmarkImportRun.user_id == user_id,
                BookmarkImportRun.job_id == job_id,
                BookmarkImportRun.id == run_id,
                BookmarkImportRun.state == "finalizing",
                BookmarkImportRun.completion_hash == completion_hash,
            )
            .values(state="running", completion_hash=None)
        )
        await session.commit()
    except OperationalError as error:
        await session.rollback()
        if _is_database_busy(error):
            return
        raise


async def _failed_parse_run_replay(
    session: AsyncSession,
    user_id: str,
    job_id: str,
    run_id: str,
    *,
    expected_job_version: int,
    failure_code: str,
) -> bool:
    row = (
        await session.execute(
            select(
                BookmarkImportJob.state,
                BookmarkImportJob.version,
                BookmarkImportJob.failure_code,
                BookmarkImportRun.state,
                BookmarkImportRun.failure_code,
            )
            .join(
                BookmarkImportRun,
                and_(
                    BookmarkImportRun.user_id == BookmarkImportJob.user_id,
                    BookmarkImportRun.job_id == BookmarkImportJob.id,
                ),
            )
            .where(
                BookmarkImportJob.user_id == user_id,
                BookmarkImportJob.id == job_id,
                BookmarkImportRun.id == run_id,
            )
        )
    ).one_or_none()
    if row is None:
        return False
    job_state, job_version, job_failure, run_state, run_failure = row
    return (
        job_state == "failed"
        and int(job_version) == expected_job_version + 1
        and job_failure == failure_code
        and run_state == "failed"
        and run_failure == failure_code
    )


async def create_import(
    session: AsyncSession,
    user_id: str,
    *,
    source_sha256: str,
    source_size_bytes: int,
    original_filename: str | None,
    idempotency_key: str,
) -> ImportJobResult:
    source_hash = _validate_digest(source_sha256, field="源文件摘要")
    if not 0 < source_size_bytes <= ParserLimits().max_file_bytes:
        raise BookmarkPersistenceValidationError("源文件大小超出书签导入限制")
    request_key_hash = _key_hash(idempotency_key, field="请求幂等键")
    existing_snapshot = await session.scalar(
        select(BookmarkImportSnapshot).where(
            BookmarkImportSnapshot.user_id == user_id,
            BookmarkImportSnapshot.request_idempotency_key_hash == request_key_hash,
        )
    )
    if existing_snapshot is not None:
        if (
            existing_snapshot.source_sha256 != source_hash
            or existing_snapshot.source_size_bytes != source_size_bytes
        ):
            raise BookmarkPersistenceConflictError("请求幂等键已绑定其他书签文件")
        existing_job = await session.scalar(
            select(BookmarkImportJob).where(
                BookmarkImportJob.user_id == user_id,
                BookmarkImportJob.snapshot_id == existing_snapshot.id,
            )
        )
        if existing_job is None:
            raise BookmarkPersistenceConflictError("书签导入任务状态不完整")
        return _job_result(existing_snapshot, existing_job, replayed=True)

    snapshot_id = new_id()
    job_id = new_id()
    now = utc_now()
    snapshot = BookmarkImportSnapshot(
        id=snapshot_id,
        user_id=user_id,
        source_sha256=source_hash,
        source_size_bytes=source_size_bytes,
        source_format="netscape_html",
        original_filename=_display_filename(original_filename),
        storage_key=f"bookmark-imports/{user_id}/{snapshot_id}/source.html",
        detected_encoding=None,
        request_idempotency_key_hash=request_key_hash,
        created_at=now,
    )
    job = BookmarkImportJob(
        id=job_id,
        user_id=user_id,
        snapshot_id=snapshot_id,
        state="queued_parse",
        parser_version=PARSER_VERSION,
        normalizer_version=NORMALIZER_VERSION,
        skill_version=SKILL_VERSION,
        version=1,
        preview_version=0,
        progress_completed=0,
        progress_total=0,
        classification_budget=0,
        classification_used=0,
        created_at=now,
        updated_at=now,
    )
    try:
        session.add(snapshot)
        await session.flush()
        session.add(job)
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        replay_snapshot = await session.scalar(
            select(BookmarkImportSnapshot).where(
                BookmarkImportSnapshot.user_id == user_id,
                BookmarkImportSnapshot.request_idempotency_key_hash == request_key_hash,
            )
        )
        if replay_snapshot is None:
            raise BookmarkPersistenceConflictError("书签导入请求发生并发冲突") from error
        if (
            replay_snapshot.source_sha256 != source_hash
            or replay_snapshot.source_size_bytes != source_size_bytes
        ):
            raise BookmarkPersistenceConflictError("请求幂等键已绑定其他书签文件") from error
        replay_job = await session.scalar(
            select(BookmarkImportJob).where(
                BookmarkImportJob.user_id == user_id,
                BookmarkImportJob.snapshot_id == replay_snapshot.id,
            )
        )
        if replay_job is None:
            raise BookmarkPersistenceConflictError("书签导入任务状态不完整") from error
        return _job_result(replay_snapshot, replay_job, replayed=True)
    except OperationalError as error:
        await session.rollback()
        if _is_database_busy(error):
            raise BookmarkPersistenceConflictError("书签导入数据库繁忙，请稍后重试") from error
        raise
    return _job_result(snapshot, job, replayed=False)


async def find_same_source(
    session: AsyncSession,
    user_id: str,
    source_sha256: str,
    *,
    limit: int = 20,
) -> list[SameSourceResult]:
    source_hash = _validate_digest(source_sha256, field="源文件摘要")
    if not 1 <= limit <= 100:
        raise BookmarkPersistenceValidationError("同源快照查询数量必须在 1 到 100 之间")
    rows = (
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
                BookmarkImportSnapshot.user_id == user_id,
                BookmarkImportSnapshot.source_sha256 == source_hash,
            )
            .order_by(BookmarkImportSnapshot.created_at.desc(), BookmarkImportSnapshot.id.desc())
            .limit(limit)
        )
    ).all()
    return [
        SameSourceResult(
            snapshot_id=snapshot.id,
            job_id=job.id,
            state=job.state,
            created_at=snapshot.created_at,
        )
        for snapshot, job in rows
    ]


async def begin_parse_run(
    session: AsyncSession,
    user_id: str,
    job_id: str,
    *,
    expected_job_version: int,
    idempotency_key: str,
) -> ParseRunResult:
    job = await _owned_job(session, user_id, job_id)
    key_hash = _key_hash(idempotency_key, field="解析运行幂等键")
    existing = await _parse_run_replay(session, user_id, job_id, key_hash)
    if existing is not None:
        return existing
    if job.version != expected_job_version:
        raise BookmarkPersistenceConflictError("书签导入任务已被修改，请刷新后重试")
    if job.state not in {"queued_parse", "parse_preview_ready", "failed"}:
        raise BookmarkPersistenceConflictError("书签导入任务当前不能开始解析")

    snapshot = await session.scalar(
        select(BookmarkImportSnapshot).where(
            BookmarkImportSnapshot.user_id == user_id,
            BookmarkImportSnapshot.id == job.snapshot_id,
        )
    )
    if snapshot is None:
        raise BookmarkPersistenceConflictError("书签源快照不存在")
    attempt_number = (
        int(
            await session.scalar(
                select(func.coalesce(func.max(BookmarkImportRun.attempt_number), 0)).where(
                    BookmarkImportRun.user_id == user_id,
                    BookmarkImportRun.job_id == job_id,
                )
            )
            or 0
        )
        + 1
    )
    try:
        claimed = await session.execute(
            update(BookmarkImportJob)
            .where(
                BookmarkImportJob.user_id == user_id,
                BookmarkImportJob.id == job_id,
                BookmarkImportJob.version == expected_job_version,
                BookmarkImportJob.state == job.state,
            )
            .values(
                state="parsing",
                version=BookmarkImportJob.version + 1,
                progress_completed=0,
                progress_total=0,
                failure_code=None,
                completed_at=None,
                updated_at=utc_now(),
            )
        )
    except OperationalError as error:
        await session.rollback()
        if _is_database_busy(error):
            raise BookmarkPersistenceConflictError("书签导入数据库繁忙，请稍后重试") from error
        raise
    if claimed.rowcount != 1:
        await session.rollback()
        replay = await _parse_run_replay(session, user_id, job_id, key_hash)
        if replay is not None:
            return replay
        raise BookmarkPersistenceConflictError("书签导入任务已被修改，请刷新后重试")

    input_hash = _sha256(
        json.dumps(
            {
                "source_sha256": snapshot.source_sha256,
                "parser_version": PARSER_VERSION,
                "normalizer_version": NORMALIZER_VERSION,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    run = BookmarkImportRun(
        id=new_id(),
        user_id=user_id,
        job_id=job_id,
        attempt_number=attempt_number,
        state="running",
        run_idempotency_key_hash=key_hash,
        input_hash=input_hash,
        parser_version=PARSER_VERSION,
        normalizer_version=NORMALIZER_VERSION,
        source_sequence_count=0,
        folder_count=0,
        occurrence_count=0,
        candidate_count=0,
        created_at=utc_now(),
    )
    session.add(run)
    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        replay = await _parse_run_replay(session, user_id, job_id, key_hash)
        if replay is None:
            raise BookmarkPersistenceConflictError("解析运行发生并发冲突") from error
        return replay
    except OperationalError as error:
        await session.rollback()
        if _is_database_busy(error):
            raise BookmarkPersistenceConflictError("书签导入数据库繁忙，请稍后重试") from error
        raise
    return ParseRunResult(
        run_id=run.id,
        attempt_number=attempt_number,
        job_version=expected_job_version + 1,
        replayed=False,
    )


def _validate_event_batch(events: Sequence[BookmarkEvent]) -> tuple[int, int]:
    if not 1 <= len(events) <= MAX_STAGE_EVENTS:
        raise BookmarkPersistenceValidationError(
            f"单个解析分片必须包含 1 到 {MAX_STAGE_EVENTS} 个事件"
        )
    sequences = [event.source_sequence for event in events]
    expected = list(range(sequences[0], sequences[0] + len(sequences)))
    if sequences != expected:
        raise BookmarkPersistenceValidationError("解析分片中的 source_sequence 必须连续递增")
    return sequences[0], sequences[-1]


async def append_parse_chunk(
    session: AsyncSession,
    user_id: str,
    job_id: str,
    run_id: str,
    *,
    chunk_index: int,
    events: Sequence[BookmarkEvent],
) -> StageChunkResult:
    if chunk_index < 0:
        raise BookmarkPersistenceValidationError("解析分片序号不能为负数")
    empty_batch = not events
    if empty_batch:
        if chunk_index != 0:
            raise BookmarkPersistenceValidationError("零事件解析检查点只能使用首个分片")
        sequence_start = 0
        sequence_end = 0
    else:
        sequence_start, sequence_end = _validate_event_batch(events)
    payload_hash = _event_batch_hash(events)
    run = await _owned_run(session, user_id, job_id, run_id)

    existing = await _parse_chunk_checkpoint(session, user_id, run_id, chunk_index)
    if existing is not None:
        return _parse_chunk_replay(existing, payload_hash=payload_hash)
    if run.state != "running":
        raise BookmarkPersistenceConflictError("解析运行已结束，不能继续写入分片")
    _assert_run_versions(run)

    previous = await session.scalar(
        select(BookmarkImportCheckpoint)
        .where(
            BookmarkImportCheckpoint.user_id == user_id,
            BookmarkImportCheckpoint.run_id == run_id,
            BookmarkImportCheckpoint.phase == "parse",
            BookmarkImportCheckpoint.state == "complete",
        )
        .order_by(BookmarkImportCheckpoint.chunk_index.desc())
        .limit(1)
    )
    if previous is None:
        if chunk_index != 0 or (not empty_batch and sequence_start != 1):
            raise BookmarkPersistenceValidationError("首个解析分片必须从 source_sequence 1 开始")
    elif (
        chunk_index != previous.chunk_index + 1
        or previous.source_sequence_end is None
        or sequence_start != previous.source_sequence_end + 1
    ):
        raise BookmarkPersistenceValidationError("解析分片必须按连续顺序写入")

    now = utc_now()
    folder_values: list[dict[str, object]] = []
    occurrence_values: list[dict[str, object]] = []
    candidate_values: list[dict[str, object]] = []
    candidate_occurrence_values: list[dict[str, object]] = []
    for event in events:
        if isinstance(event, ParsedFolder):
            source_key = str(event.source_folder_id)
            folder_values.append(
                {
                    "id": _stable_id(run_id, "folder", source_key),
                    "user_id": user_id,
                    "run_id": run_id,
                    "source_folder_key": source_key,
                    "parent_id": (
                        _stable_id(run_id, "folder", str(event.parent_source_folder_id))
                        if event.parent_source_folder_id is not None
                        else None
                    ),
                    "source_sequence": event.source_sequence,
                    "source_order": event.source_order,
                    "depth": event.depth,
                    "title": event.title,
                    "display_path": json.dumps(
                        event.folder_path,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    "created_at": now,
                }
            )
            continue

        normalized = normalize_bookmark_url(event.raw_url)
        has_parser_issue = bool(event.issues)
        validation_status = (
            NormalizationStatus.INVALID.value if has_parser_issue else normalized.status.value
        )
        reason_code = f"parser:{event.issues[0]}" if has_parser_issue else normalized.reason
        occurrence_id = _stable_id(run_id, "occurrence", str(event.position))
        folder_id = (
            _stable_id(run_id, "folder", str(event.source_folder_id))
            if event.source_folder_id is not None
            else None
        )
        identity_url = normalized.normalized_url if not has_parser_issue else None
        has_sensitive_url = bool(identity_url and sensitive_url_keys(identity_url))
        fetch_policy = (
            normalized.fetch_policy.value
            if not has_parser_issue and normalized.fetch_policy is not None
            else None
        )
        occurrence_values.append(
            {
                "id": occurrence_id,
                "user_id": user_id,
                "run_id": run_id,
                "source_occurrence_key": str(event.position),
                "folder_id": folder_id,
                "source_sequence": event.source_sequence,
                "source_order": event.position,
                "raw_title": event.title,
                "raw_url": event.raw_url,
                "add_date": event.add_date,
                "last_modified": event.last_modified,
                "validation_status": validation_status,
                "fetch_policy": fetch_policy,
                "reason_code": reason_code,
                "has_sensitive_url": has_sensitive_url,
                "created_at": now,
            }
        )
        if validation_status != NormalizationStatus.ACCEPTED.value or identity_url is None:
            continue
        assert normalized.host is not None
        assert fetch_policy is not None
        identity_hash = _sha256(identity_url)
        candidate_id = _stable_id(run_id, "candidate", f"{identity_hash}:{identity_url}")
        candidate_values.append(
            {
                "id": candidate_id,
                "user_id": user_id,
                "run_id": run_id,
                "identity_url": identity_url,
                "identity_hash": identity_hash,
                "display_title": event.title or normalized.host,
                "host": normalized.host,
                "fetch_policy": fetch_policy,
                "has_sensitive_url": has_sensitive_url,
                "proposed_action": "create",
                "occurrence_count": 1,
                "first_source_sequence": event.source_sequence,
                "created_at": now,
            }
        )
        candidate_occurrence_values.append(
            {
                "user_id": user_id,
                "run_id": run_id,
                "candidate_id": candidate_id,
                "occurrence_id": occurrence_id,
            }
        )

    try:
        if folder_values:
            await session.execute(sqlite_insert(BookmarkStagingFolder), folder_values)
        if occurrence_values:
            await session.execute(sqlite_insert(BookmarkStagingOccurrence), occurrence_values)
        if candidate_values:
            candidate_insert = sqlite_insert(BookmarkStagingCandidate)
            await session.execute(
                candidate_insert.on_conflict_do_update(
                    index_elements=[
                        BookmarkStagingCandidate.user_id,
                        BookmarkStagingCandidate.run_id,
                        BookmarkStagingCandidate.identity_hash,
                        BookmarkStagingCandidate.identity_url,
                    ],
                    set_={
                        "display_title": case(
                            (
                                func.length(candidate_insert.excluded.display_title)
                                > func.length(BookmarkStagingCandidate.display_title),
                                candidate_insert.excluded.display_title,
                            ),
                            else_=BookmarkStagingCandidate.display_title,
                        ),
                        "has_sensitive_url": case(
                            (candidate_insert.excluded.has_sensitive_url.is_(True), True),
                            else_=BookmarkStagingCandidate.has_sensitive_url,
                        ),
                        "fetch_policy": case(
                            (
                                candidate_insert.excluded.fetch_policy == "export_metadata_only",
                                "export_metadata_only",
                            ),
                            else_=BookmarkStagingCandidate.fetch_policy,
                        ),
                        "first_source_sequence": func.min(
                            BookmarkStagingCandidate.first_source_sequence,
                            candidate_insert.excluded.first_source_sequence,
                        ),
                    },
                ),
                candidate_values,
            )
        if candidate_occurrence_values:
            await session.execute(
                sqlite_insert(BookmarkStagingCandidateOccurrence),
                candidate_occurrence_values,
            )
        checkpoint = BookmarkImportCheckpoint(
            id=new_id(),
            user_id=user_id,
            run_id=run_id,
            phase="parse",
            chunk_index=chunk_index,
            idempotency_key_hash=_sha256(f"{run_id}:parse:{chunk_index}"),
            input_hash=payload_hash,
            state="complete",
            source_sequence_start=None if empty_batch else sequence_start,
            source_sequence_end=None if empty_batch else sequence_end,
            processed_count=len(events),
            created_at=now,
            updated_at=now,
            completed_at=now,
        )
        session.add(checkpoint)
        await session.execute(
            update(BookmarkImportJob)
            .where(
                BookmarkImportJob.user_id == user_id,
                BookmarkImportJob.id == job_id,
                BookmarkImportJob.state == "parsing",
            )
            .values(progress_completed=sequence_end, updated_at=now)
        )
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        replay = await _parse_chunk_checkpoint(session, user_id, run_id, chunk_index)
        if replay is not None:
            return _parse_chunk_replay(replay, payload_hash=payload_hash)
        raise BookmarkPersistenceConflictError("解析分片与已暂存数据冲突") from error
    except OperationalError as error:
        await session.rollback()
        if _is_database_busy(error):
            raise BookmarkPersistenceConflictError("书签导入数据库繁忙，请稍后重试") from error
        raise

    return StageChunkResult(
        run_id=run_id,
        chunk_index=chunk_index,
        source_sequence_start=sequence_start,
        source_sequence_end=sequence_end,
        processed_count=len(events),
        payload_hash=payload_hash,
        replayed=False,
    )


async def _validate_complete_staging(
    session: AsyncSession,
    user_id: str,
    run_id: str,
    expected: ParseCompletion,
) -> None:
    checkpoints = list(
        (
            await session.scalars(
                select(BookmarkImportCheckpoint)
                .where(
                    BookmarkImportCheckpoint.user_id == user_id,
                    BookmarkImportCheckpoint.run_id == run_id,
                    BookmarkImportCheckpoint.phase == "parse",
                )
                .order_by(BookmarkImportCheckpoint.chunk_index)
            )
        ).all()
    )
    if expected.source_sequence_count == 0:
        if (
            len(checkpoints) != 1
            or checkpoints[0].chunk_index != 0
            or checkpoints[0].state != "complete"
            or checkpoints[0].input_hash != _event_batch_hash(())
            or checkpoints[0].source_sequence_start is not None
            or checkpoints[0].source_sequence_end is not None
            or checkpoints[0].processed_count != 0
        ):
            raise BookmarkPersistenceValidationError("零事件解析运行缺少完成检查点")
        staged_tables = (
            BookmarkStagingFolder,
            BookmarkStagingOccurrence,
            BookmarkStagingCandidate,
            BookmarkStagingCandidateOccurrence,
            BookmarkStagingCandidateFolder,
            BookmarkStagingCandidateSiteMatch,
        )
        for table in staged_tables:
            staged_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(table)
                    .where(table.user_id == user_id, table.run_id == run_id)
                )
                or 0
            )
            if staged_count:
                raise BookmarkPersistenceValidationError("零事件解析运行包含意外的暂存数据")
        return
    if not checkpoints:
        raise BookmarkPersistenceValidationError("解析运行没有已完成的分片")
    next_sequence = 1
    for expected_chunk_index, checkpoint in enumerate(checkpoints):
        if (
            checkpoint.chunk_index != expected_chunk_index
            or checkpoint.state != "complete"
            or checkpoint.source_sequence_start != next_sequence
            or checkpoint.source_sequence_end is None
        ):
            raise BookmarkPersistenceValidationError("解析分片检查点不连续或未完成")
        next_sequence = checkpoint.source_sequence_end + 1

    folder_sequences = select(BookmarkStagingFolder.source_sequence.label("source_sequence")).where(
        BookmarkStagingFolder.user_id == user_id,
        BookmarkStagingFolder.run_id == run_id,
    )
    occurrence_sequences = select(
        BookmarkStagingOccurrence.source_sequence.label("source_sequence")
    ).where(
        BookmarkStagingOccurrence.user_id == user_id,
        BookmarkStagingOccurrence.run_id == run_id,
    )
    combined = union_all(folder_sequences, occurrence_sequences).subquery()
    row = (
        await session.execute(
            select(
                func.count().label("total"),
                func.count(func.distinct(combined.c.source_sequence)).label("distinct_total"),
                func.min(combined.c.source_sequence).label("minimum"),
                func.max(combined.c.source_sequence).label("maximum"),
            ).select_from(combined)
        )
    ).one()
    if (
        row.total != expected.source_sequence_count
        or row.distinct_total != expected.source_sequence_count
        or row.minimum != 1
        or row.maximum != expected.source_sequence_count
        or next_sequence != expected.source_sequence_count + 1
    ):
        raise BookmarkPersistenceValidationError("暂存事件的 source_sequence 不完整")
    folder_count = int(
        await session.scalar(
            select(func.count())
            .select_from(BookmarkStagingFolder)
            .where(
                BookmarkStagingFolder.user_id == user_id,
                BookmarkStagingFolder.run_id == run_id,
            )
        )
        or 0
    )
    occurrence_count = int(
        await session.scalar(
            select(func.count())
            .select_from(BookmarkStagingOccurrence)
            .where(
                BookmarkStagingOccurrence.user_id == user_id,
                BookmarkStagingOccurrence.run_id == run_id,
            )
        )
        or 0
    )
    if folder_count != expected.folder_count or occurrence_count != expected.occurrence_count:
        raise BookmarkPersistenceValidationError("暂存目录或 occurrence 数量与 parser 统计不一致")

    accepted_count = int(
        await session.scalar(
            select(func.count())
            .select_from(BookmarkStagingOccurrence)
            .where(
                BookmarkStagingOccurrence.user_id == user_id,
                BookmarkStagingOccurrence.run_id == run_id,
                BookmarkStagingOccurrence.validation_status == NormalizationStatus.ACCEPTED.value,
            )
        )
        or 0
    )
    linked_count = int(
        await session.scalar(
            select(func.count())
            .select_from(BookmarkStagingCandidateOccurrence)
            .where(
                BookmarkStagingCandidateOccurrence.user_id == user_id,
                BookmarkStagingCandidateOccurrence.run_id == run_id,
            )
        )
        or 0
    )
    if linked_count != accepted_count:
        raise BookmarkPersistenceValidationError("accepted occurrence 与 candidate 投影不一致")

    candidate_count = int(
        await session.scalar(
            select(func.count())
            .select_from(BookmarkStagingCandidate)
            .where(
                BookmarkStagingCandidate.user_id == user_id,
                BookmarkStagingCandidate.run_id == run_id,
            )
        )
        or 0
    )
    linked_candidate_count = int(
        await session.scalar(
            select(func.count(func.distinct(BookmarkStagingCandidateOccurrence.candidate_id))).where(
                BookmarkStagingCandidateOccurrence.user_id == user_id,
                BookmarkStagingCandidateOccurrence.run_id == run_id,
            )
        )
        or 0
    )
    if candidate_count != linked_candidate_count:
        raise BookmarkPersistenceValidationError("candidate 投影包含无来源项")

    projection_rows = await session.stream(
        select(
            BookmarkStagingOccurrence.raw_url,
            BookmarkStagingOccurrence.fetch_policy,
            BookmarkStagingOccurrence.has_sensitive_url,
            BookmarkStagingCandidate.identity_url,
            BookmarkStagingCandidate.identity_hash,
            BookmarkStagingCandidate.host,
            BookmarkStagingCandidate.fetch_policy,
            BookmarkStagingCandidate.has_sensitive_url,
        )
        .select_from(BookmarkStagingCandidateOccurrence)
        .join(
            BookmarkStagingOccurrence,
            and_(
                BookmarkStagingOccurrence.user_id == BookmarkStagingCandidateOccurrence.user_id,
                BookmarkStagingOccurrence.run_id == BookmarkStagingCandidateOccurrence.run_id,
                BookmarkStagingOccurrence.id == BookmarkStagingCandidateOccurrence.occurrence_id,
            ),
        )
        .join(
            BookmarkStagingCandidate,
            and_(
                BookmarkStagingCandidate.user_id == BookmarkStagingCandidateOccurrence.user_id,
                BookmarkStagingCandidate.run_id == BookmarkStagingCandidateOccurrence.run_id,
                BookmarkStagingCandidate.id == BookmarkStagingCandidateOccurrence.candidate_id,
            ),
        )
        .where(
            BookmarkStagingCandidateOccurrence.user_id == user_id,
            BookmarkStagingCandidateOccurrence.run_id == run_id,
        )
        .execution_options(yield_per=1_000)
    )
    async for row in projection_rows:
        (
            raw_url,
            occurrence_fetch_policy,
            occurrence_sensitive,
            identity_url,
            identity_hash,
            host,
            candidate_fetch_policy,
            candidate_sensitive,
        ) = row
        normalized = normalize_bookmark_url(raw_url)
        expected_fetch_policy = (
            normalized.fetch_policy.value if normalized.fetch_policy is not None else None
        )
        expected_sensitive = bool(
            normalized.normalized_url and sensitive_url_keys(normalized.normalized_url)
        )
        if (
            normalized.status is not NormalizationStatus.ACCEPTED
            or normalized.normalized_url != identity_url
            or _sha256(identity_url) != identity_hash
            or normalized.host != host
            or occurrence_fetch_policy != expected_fetch_policy
            or candidate_fetch_policy != expected_fetch_policy
            or bool(occurrence_sensitive) != expected_sensitive
            or bool(candidate_sensitive) != expected_sensitive
        ):
            raise BookmarkPersistenceValidationError(
                "occurrence 与 candidate identity 投影不一致"
            )


async def _staged_completion(
    session: AsyncSession,
    user_id: str,
    job_id: str,
    run_id: str,
) -> ParseCompletion:
    job = await _owned_job(session, user_id, job_id)
    snapshot = await session.scalar(
        select(BookmarkImportSnapshot).where(
            BookmarkImportSnapshot.user_id == user_id,
            BookmarkImportSnapshot.id == job.snapshot_id,
        )
    )
    if snapshot is None:
        raise BookmarkPersistenceConflictError("书签源快照不存在")
    folder_count = int(
        await session.scalar(
            select(func.count())
            .select_from(BookmarkStagingFolder)
            .where(
                BookmarkStagingFolder.user_id == user_id,
                BookmarkStagingFolder.run_id == run_id,
            )
        )
        or 0
    )
    occurrence_count = int(
        await session.scalar(
            select(func.count())
            .select_from(BookmarkStagingOccurrence)
            .where(
                BookmarkStagingOccurrence.user_id == user_id,
                BookmarkStagingOccurrence.run_id == run_id,
            )
        )
        or 0
    )
    return ParseCompletion(
        source_sha256=snapshot.source_sha256,
        source_sequence_count=folder_count + occurrence_count,
        folder_count=folder_count,
        occurrence_count=occurrence_count,
    )


async def _rebuild_candidate_projections(
    session: AsyncSession,
    user_id: str,
    run_id: str,
    now: datetime,
) -> int:
    candidate_aggregates = (
        select(
            BookmarkStagingCandidateOccurrence.candidate_id.label("candidate_id"),
            func.count().label("occurrence_count"),
            func.min(BookmarkStagingOccurrence.source_sequence).label("first_source_sequence"),
        )
        .join(
            BookmarkStagingOccurrence,
            and_(
                BookmarkStagingOccurrence.user_id == BookmarkStagingCandidateOccurrence.user_id,
                BookmarkStagingOccurrence.run_id == BookmarkStagingCandidateOccurrence.run_id,
                BookmarkStagingOccurrence.id == BookmarkStagingCandidateOccurrence.occurrence_id,
            ),
        )
        .where(
            BookmarkStagingCandidateOccurrence.user_id == user_id,
            BookmarkStagingCandidateOccurrence.run_id == run_id,
        )
        .group_by(BookmarkStagingCandidateOccurrence.candidate_id)
        .subquery()
    )
    await session.execute(
        update(BookmarkStagingCandidate)
        .where(
            BookmarkStagingCandidate.user_id == user_id,
            BookmarkStagingCandidate.run_id == run_id,
            BookmarkStagingCandidate.id == candidate_aggregates.c.candidate_id,
        )
        .values(
            occurrence_count=candidate_aggregates.c.occurrence_count,
            first_source_sequence=candidate_aggregates.c.first_source_sequence,
        )
    )

    await session.execute(
        delete(BookmarkStagingCandidateFolder).where(
            BookmarkStagingCandidateFolder.user_id == user_id,
            BookmarkStagingCandidateFolder.run_id == run_id,
        )
    )
    folder_scope_key = func.coalesce(BookmarkStagingOccurrence.folder_id, "root")
    candidate_folder_projection = (
        select(
            BookmarkStagingCandidateOccurrence.user_id,
            BookmarkStagingCandidateOccurrence.run_id,
            BookmarkStagingCandidateOccurrence.candidate_id,
            folder_scope_key.label("folder_scope_key"),
            BookmarkStagingOccurrence.folder_id,
            func.count().label("occurrence_count"),
            func.min(BookmarkStagingOccurrence.source_sequence).label("first_source_sequence"),
        )
        .join(
            BookmarkStagingOccurrence,
            and_(
                BookmarkStagingOccurrence.user_id == BookmarkStagingCandidateOccurrence.user_id,
                BookmarkStagingOccurrence.run_id == BookmarkStagingCandidateOccurrence.run_id,
                BookmarkStagingOccurrence.id == BookmarkStagingCandidateOccurrence.occurrence_id,
            ),
        )
        .where(
            BookmarkStagingCandidateOccurrence.user_id == user_id,
            BookmarkStagingCandidateOccurrence.run_id == run_id,
        )
        .group_by(
            BookmarkStagingCandidateOccurrence.user_id,
            BookmarkStagingCandidateOccurrence.run_id,
            BookmarkStagingCandidateOccurrence.candidate_id,
            BookmarkStagingOccurrence.folder_id,
        )
    )
    await session.execute(
        sqlite_insert(BookmarkStagingCandidateFolder).from_select(
            [
                "user_id",
                "run_id",
                "candidate_id",
                "folder_scope_key",
                "folder_id",
                "occurrence_count",
                "first_source_sequence",
            ],
            candidate_folder_projection,
        )
    )

    await session.execute(
        delete(BookmarkStagingCandidateSiteMatch).where(
            BookmarkStagingCandidateSiteMatch.user_id == user_id,
            BookmarkStagingCandidateSiteMatch.run_id == run_id,
        )
    )
    site_matches = (
        select(
            BookmarkStagingCandidate.user_id,
            BookmarkStagingCandidate.run_id,
            BookmarkStagingCandidate.id,
            Site.id,
            Site.version,
            literal(now),
        )
        .join(
            Site,
            and_(
                Site.user_id == BookmarkStagingCandidate.user_id,
                Site.identity_url == BookmarkStagingCandidate.identity_url,
            ),
        )
        .where(
            BookmarkStagingCandidate.user_id == user_id,
            BookmarkStagingCandidate.run_id == run_id,
        )
    )
    await session.execute(
        sqlite_insert(BookmarkStagingCandidateSiteMatch).from_select(
            ["user_id", "run_id", "candidate_id", "site_id", "site_version", "matched_at"],
            site_matches,
        )
    )
    matched_site = select(BookmarkStagingCandidateSiteMatch.candidate_id).where(
        BookmarkStagingCandidateSiteMatch.user_id == user_id,
        BookmarkStagingCandidateSiteMatch.run_id == run_id,
        BookmarkStagingCandidateSiteMatch.candidate_id == BookmarkStagingCandidate.id,
    )
    await session.execute(
        update(BookmarkStagingCandidate)
        .where(
            BookmarkStagingCandidate.user_id == user_id,
            BookmarkStagingCandidate.run_id == run_id,
        )
        .values(
            proposed_action=case(
                (matched_site.exists(), "skip_existing"),
                else_="create",
            )
        )
    )
    return int(
        await session.scalar(
            select(func.count())
            .select_from(BookmarkStagingCandidate)
            .where(
                BookmarkStagingCandidate.user_id == user_id,
                BookmarkStagingCandidate.run_id == run_id,
            )
        )
        or 0
    )


async def finalize_parse_run(
    session: AsyncSession,
    user_id: str,
    job_id: str,
    run_id: str,
    *,
    expected_job_version: int,
    completion: ParseCompletion,
) -> ParsePreviewSummary:
    source_hash = _validate_digest(completion.source_sha256, field="parser 源文件摘要")
    if (
        min(
            completion.source_sequence_count,
            completion.folder_count,
            completion.occurrence_count,
        )
        < 0
    ):
        raise BookmarkPersistenceValidationError("parser 完成统计不能为负数")
    if completion.source_sequence_count != (completion.folder_count + completion.occurrence_count):
        raise BookmarkPersistenceValidationError("parser 完成统计中的事件数量不一致")
    completion_hash = _parse_completion_hash(source_hash, completion)

    job = await _owned_job(session, user_id, job_id)
    run = await _owned_run(session, user_id, job_id, run_id)
    if run.state == "complete":
        replay = await _completed_parse_preview_replay(
            session,
            user_id,
            job_id,
            run_id,
            expected_job_version=expected_job_version,
            completion_hash=completion_hash,
        )
        if replay is not None:
            return replay
        raise BookmarkPersistenceConflictError("解析运行已按不同状态发布")
    if run.state in {"running", "finalizing"}:
        _assert_run_versions(run)
    if job.version != expected_job_version or job.state != "parsing":
        raise BookmarkPersistenceConflictError("书签导入任务已被修改，请刷新后重试")
    resume_finalizing = run.state == "finalizing"
    if resume_finalizing and run.completion_hash != completion_hash:
        raise BookmarkPersistenceConflictError("解析运行正在按不同统计发布")
    if run.state not in {"running", "finalizing"}:
        raise BookmarkPersistenceConflictError("解析运行已结束")
    snapshot = await session.scalar(
        select(BookmarkImportSnapshot).where(
            BookmarkImportSnapshot.user_id == user_id,
            BookmarkImportSnapshot.id == job.snapshot_id,
        )
    )
    if snapshot is None or snapshot.source_sha256 != source_hash:
        raise BookmarkPersistenceValidationError("parser 读取的文件与源快照摘要不一致")

    if not resume_finalizing:
        try:
            sealed_run = await session.execute(
                update(BookmarkImportRun)
                .where(
                    BookmarkImportRun.user_id == user_id,
                    BookmarkImportRun.job_id == job_id,
                    BookmarkImportRun.id == run_id,
                    BookmarkImportRun.state == "running",
                )
                .values(state="finalizing", completion_hash=completion_hash)
            )
            if sealed_run.rowcount != 1:
                await session.rollback()
                replay = await _completed_parse_preview_replay(
                    session,
                    user_id,
                    job_id,
                    run_id,
                    expected_job_version=expected_job_version,
                    completion_hash=completion_hash,
                )
                if replay is not None:
                    return replay
                raise BookmarkPersistenceConflictError("解析运行已被其他请求封口")
            await session.commit()
        except BookmarkPersistenceError:
            await session.rollback()
            raise
        except IntegrityError as error:
            await session.rollback()
            raise BookmarkPersistenceConflictError("封口解析运行时发生数据冲突") from error
        except OperationalError as error:
            await session.rollback()
            if _is_database_busy(error):
                raise BookmarkPersistenceConflictError("书签导入数据库繁忙，请稍后重试") from error
            raise

    now = utc_now()
    next_preview_version = job.preview_version + 1
    try:
        await _validate_complete_staging(session, user_id, run_id, completion)
        candidate_count = await _rebuild_candidate_projections(
            session,
            user_id,
            run_id,
            now,
        )
        completed_run = await session.execute(
            update(BookmarkImportRun)
            .where(
                BookmarkImportRun.user_id == user_id,
                BookmarkImportRun.job_id == job_id,
                BookmarkImportRun.id == run_id,
                BookmarkImportRun.state == "finalizing",
                BookmarkImportRun.completion_hash == completion_hash,
            )
            .values(
                state="complete",
                source_sequence_count=completion.source_sequence_count,
                folder_count=completion.folder_count,
                occurrence_count=completion.occurrence_count,
                candidate_count=candidate_count,
                completed_at=now,
            )
        )
        if completed_run.rowcount != 1:
            raise BookmarkPersistenceConflictError("解析运行已被其他请求结束")
        completed_job = await session.execute(
            update(BookmarkImportJob)
            .where(
                BookmarkImportJob.user_id == user_id,
                BookmarkImportJob.id == job_id,
                BookmarkImportJob.version == expected_job_version,
                BookmarkImportJob.state == "parsing",
            )
            .values(
                state="parse_preview_ready",
                version=BookmarkImportJob.version + 1,
                preview_version=next_preview_version,
                progress_completed=completion.source_sequence_count,
                progress_total=completion.source_sequence_count,
                updated_at=now,
            )
        )
        if completed_job.rowcount != 1:
            raise BookmarkPersistenceConflictError("书签导入任务已被其他请求修改")
        current_run = sqlite_insert(BookmarkImportCurrentRun).values(
            user_id=user_id,
            job_id=job_id,
            run_id=run_id,
            switched_at=now,
        )
        await session.execute(
            current_run.on_conflict_do_update(
                index_elements=[
                    BookmarkImportCurrentRun.user_id,
                    BookmarkImportCurrentRun.job_id,
                ],
                set_={"run_id": run_id, "switched_at": now},
            )
        )
        await session.commit()
    except BookmarkPersistenceError:
        await session.rollback()
        if not resume_finalizing:
            await _release_parse_run_seal(
                session,
                user_id,
                job_id,
                run_id,
                completion_hash,
            )
        raise
    except IntegrityError as error:
        await session.rollback()
        if not resume_finalizing:
            await _release_parse_run_seal(
                session,
                user_id,
                job_id,
                run_id,
                completion_hash,
            )
        raise BookmarkPersistenceConflictError("发布解析预览时发生数据冲突") from error
    except OperationalError as error:
        await session.rollback()
        if not resume_finalizing:
            await _release_parse_run_seal(
                session,
                user_id,
                job_id,
                run_id,
                completion_hash,
            )
        if _is_database_busy(error):
            raise BookmarkPersistenceConflictError("书签导入数据库繁忙，请稍后重试") from error
        raise

    return ParsePreviewSummary(
        job_id=job_id,
        run_id=run_id,
        job_version=expected_job_version + 1,
        preview_version=next_preview_version,
        source_sequence_count=completion.source_sequence_count,
        folder_count=completion.folder_count,
        occurrence_count=completion.occurrence_count,
        candidate_count=candidate_count,
    )


async def recover_finalizing_parse_run(
    session: AsyncSession,
    user_id: str,
    job_id: str,
    run_id: str,
    *,
    expected_job_version: int,
) -> ParsePreviewSummary:
    """Resume a durable finalizing run after a worker restart.

    The completion counts are reconstructed from immutable staging facts, so a
    recovery worker does not need to retain the crashed parser's in-memory
    completion payload.
    """
    run = await _owned_run(session, user_id, job_id, run_id)
    if run.state != "finalizing" or run.completion_hash is None:
        raise BookmarkPersistenceConflictError("解析运行当前不需要恢复")
    _assert_run_versions(run)
    completion = await _staged_completion(session, user_id, job_id, run_id)
    if _parse_completion_hash(completion.source_sha256, completion) != run.completion_hash:
        raise BookmarkPersistenceConflictError("封口统计与暂存事实不一致，请重新开始解析")
    return await finalize_parse_run(
        session,
        user_id,
        job_id,
        run_id,
        expected_job_version=expected_job_version,
        completion=completion,
    )


async def fail_parse_run(
    session: AsyncSession,
    user_id: str,
    job_id: str,
    run_id: str,
    *,
    expected_job_version: int,
    failure_code: str,
) -> None:
    code = failure_code.strip().casefold()
    if not re.fullmatch(r"[a-z0-9_.-]{1,64}", code):
        raise BookmarkPersistenceValidationError("失败原因码格式无效")
    job = await _owned_job(session, user_id, job_id)
    run = await _owned_run(session, user_id, job_id, run_id)
    if run.state == "failed":
        if await _failed_parse_run_replay(
            session,
            user_id,
            job_id,
            run_id,
            expected_job_version=expected_job_version,
            failure_code=code,
        ):
            return
        raise BookmarkPersistenceConflictError("解析运行已按其他失败状态结束")
    if (
        job.version != expected_job_version
        or job.state != "parsing"
        or run.state not in {"running", "finalizing"}
    ):
        raise BookmarkPersistenceConflictError("解析运行已被修改")
    now = utc_now()
    try:
        run_result = await session.execute(
            update(BookmarkImportRun)
            .where(
                BookmarkImportRun.user_id == user_id,
                BookmarkImportRun.job_id == job_id,
                BookmarkImportRun.id == run_id,
                BookmarkImportRun.state.in_(("running", "finalizing")),
            )
            .values(state="failed", failure_code=code, completed_at=now)
        )
        job_result = await session.execute(
            update(BookmarkImportJob)
            .where(
                BookmarkImportJob.user_id == user_id,
                BookmarkImportJob.id == job_id,
                BookmarkImportJob.version == expected_job_version,
                BookmarkImportJob.state == "parsing",
            )
            .values(
                state="failed",
                version=BookmarkImportJob.version + 1,
                failure_code=code,
                updated_at=now,
                completed_at=now,
            )
        )
        if run_result.rowcount != 1 or job_result.rowcount != 1:
            await session.rollback()
            if await _failed_parse_run_replay(
                session,
                user_id,
                job_id,
                run_id,
                expected_job_version=expected_job_version,
                failure_code=code,
            ):
                return
            raise BookmarkPersistenceConflictError("解析运行已被其他请求修改")
        await session.commit()
    except BookmarkPersistenceError:
        await session.rollback()
        raise
    except IntegrityError as error:
        await session.rollback()
        if await _failed_parse_run_replay(
            session,
            user_id,
            job_id,
            run_id,
            expected_job_version=expected_job_version,
            failure_code=code,
        ):
            return
        raise BookmarkPersistenceConflictError("结束解析运行时发生数据冲突") from error
    except OperationalError as error:
        await session.rollback()
        if _is_database_busy(error):
            raise BookmarkPersistenceConflictError("书签导入数据库繁忙，请稍后重试") from error
        raise


async def get_current_preview_summary(
    session: AsyncSession,
    user_id: str,
    job_id: str,
) -> ParsePreviewSummary:
    job = await _owned_job(session, user_id, job_id)
    row = (
        await session.execute(
            select(BookmarkImportCurrentRun, BookmarkImportRun)
            .join(
                BookmarkImportRun,
                and_(
                    BookmarkImportRun.user_id == BookmarkImportCurrentRun.user_id,
                    BookmarkImportRun.job_id == BookmarkImportCurrentRun.job_id,
                    BookmarkImportRun.id == BookmarkImportCurrentRun.run_id,
                ),
            )
            .where(
                BookmarkImportCurrentRun.user_id == user_id,
                BookmarkImportCurrentRun.job_id == job_id,
            )
        )
    ).one_or_none()
    if row is None:
        raise BookmarkPersistenceNotFoundError("书签解析预览尚未生成")
    _, run = row
    return ParsePreviewSummary(
        job_id=job_id,
        run_id=run.id,
        job_version=job.version,
        preview_version=job.preview_version,
        source_sequence_count=run.source_sequence_count,
        folder_count=run.folder_count,
        occurrence_count=run.occurrence_count,
        candidate_count=run.candidate_count,
    )
