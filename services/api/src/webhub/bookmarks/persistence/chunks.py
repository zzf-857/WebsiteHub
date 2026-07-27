from __future__ import annotations

import json
from collections.abc import Sequence

from sqlalchemy import case, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.bookmarks.models import (
    BookmarkEvent,
    NormalizationStatus,
    ParsedFolder,
)
from webhub.bookmarks.normalization import normalize_bookmark_url
from webhub.bookmarks.privacy import sensitive_url_keys
from webhub.db.models import (
    BookmarkImportCheckpoint,
    BookmarkImportJob,
    BookmarkStagingCandidate,
    BookmarkStagingCandidateOccurrence,
    BookmarkStagingFolder,
    BookmarkStagingOccurrence,
    new_id,
    utc_now,
)

# 限定访问的理由同 runs.py：从 ._common import 会在导入时绑死，
# 测试 patch 一处就覆盖不到这里。
from . import _common
from ._common import (
    MAX_STAGE_EVENTS,
    BookmarkPersistenceConflictError,
    BookmarkPersistenceValidationError,
    StageChunkResult,
    _assert_run_versions,
    _event_batch_hash,
    _is_database_busy,
    _owned_run,
    _parse_chunk_replay,
    _sha256,
    _stable_id,
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

    existing = await _common._parse_chunk_checkpoint(session, user_id, run_id, chunk_index)
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
        replay = await _common._parse_chunk_checkpoint(session, user_id, run_id, chunk_index)
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
