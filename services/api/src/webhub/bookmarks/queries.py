from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Literal, cast

from sqlalchemy import and_, case, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.bookmarks import persistence
from webhub.bookmarks.apply import apply_candidates
from webhub.bookmarks.schemas import (
    BookmarkImportApplyResponse,
    BookmarkImportFailureCode,
    BookmarkImportProgressResponse,
    BookmarkImportStatusResponse,
    BookmarkPreviewCandidateActionCounts,
    BookmarkPreviewCandidatePageResponse,
    BookmarkPreviewCandidateResponse,
    BookmarkPreviewFolderPageResponse,
    BookmarkPreviewFolderResponse,
    BookmarkPreviewOccurrenceCounts,
    BookmarkPreviewOccurrencePageResponse,
    BookmarkPreviewOccurrenceResponse,
    BookmarkPreviewSummaryResponse,
    ProposedAction,
    ValidationStatus,
)
from webhub.db.models import (
    BookmarkImportCurrentRun,
    BookmarkImportJob,
    BookmarkImportRun,
    BookmarkStagingCandidate,
    BookmarkStagingFolder,
    BookmarkStagingOccurrence,
    utc_now,
)

PreviewEndpoint = Literal["folders", "candidates", "occurrences"]
_PUBLIC_FAILURE_CODES: frozenset[BookmarkImportFailureCode] = frozenset(
    {
        "classification_budget_exhausted",
        "internal_error",
        "invalid_bookmark_file",
        "processing_limit_exceeded",
    }
)


@dataclass(frozen=True, slots=True)
class _CurrentPreview:
    job: BookmarkImportJob
    run: BookmarkImportRun


def _public_failure_code(value: str | None) -> BookmarkImportFailureCode | None:
    if value is None:
        return None
    if value in _PUBLIC_FAILURE_CODES:
        return cast(BookmarkImportFailureCode, value)
    return "internal_error"


async def get_import_status(
    session: AsyncSession,
    user_id: str,
    job_id: str,
) -> BookmarkImportStatusResponse:
    row = (
        await session.execute(
            select(
                BookmarkImportJob.id,
                BookmarkImportJob.state,
                BookmarkImportJob.version,
                BookmarkImportJob.preview_version,
                BookmarkImportJob.progress_completed,
                BookmarkImportJob.progress_total,
                BookmarkImportJob.failure_code,
                BookmarkImportJob.created_at,
                BookmarkImportJob.updated_at,
                BookmarkImportJob.completed_at,
            ).where(
                BookmarkImportJob.user_id == user_id,
                BookmarkImportJob.id == job_id,
            )
        )
    ).one_or_none()
    if row is None:
        raise persistence.BookmarkPersistenceNotFoundError("书签导入任务不存在")

    (
        owned_job_id,
        state,
        job_version,
        preview_version,
        progress_completed,
        progress_total,
        failure_code,
        created_at,
        updated_at,
        completed_at,
    ) = row
    return BookmarkImportStatusResponse(
        job_id=owned_job_id,
        state=state,
        job_version=job_version,
        preview_version=preview_version,
        progress=BookmarkImportProgressResponse(
            completed=progress_completed,
            total=progress_total,
        ),
        failure_code=_public_failure_code(failure_code),
        created_at=created_at,
        updated_at=updated_at,
        completed_at=completed_at,
    )


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _cursor_scope(
    *,
    user_id: str,
    job_id: str,
    run_id: str,
    job_version: int,
    preview_version: int,
    endpoint: PreviewEndpoint,
    filters: dict[str, str | None],
) -> str:
    return hashlib.sha256(
        _canonical_json(
            {
                "user_id": user_id,
                "job_id": job_id,
                "run_id": run_id,
                "job_version": job_version,
                "preview_version": preview_version,
                "endpoint": endpoint,
                "filters": filters,
            }
        )
    ).hexdigest()


def _encode_cursor(*, sequence: int, item_id: str, scope: str) -> str:
    payload = {
        "v": 1,
        "sequence": sequence,
        "id": item_id,
        "scope": scope,
    }
    envelope = {
        "payload": payload,
        "checksum": hashlib.sha256(_canonical_json(payload)).hexdigest(),
    }
    return base64.urlsafe_b64encode(_canonical_json(envelope)).decode().rstrip("=")


def _decode_cursor(cursor: str, *, scope: str) -> tuple[int, str]:
    try:
        padding = "=" * (-len(cursor) % 4)
        decoded = base64.b64decode(
            cursor + padding,
            altchars=b"-_",
            validate=True,
        )
        envelope = json.loads(decoded)
        if not isinstance(envelope, dict) or set(envelope) != {"payload", "checksum"}:
            raise ValueError
        payload = envelope["payload"]
        checksum = envelope["checksum"]
        if (
            not isinstance(payload, dict)
            or set(payload) != {"v", "sequence", "id", "scope"}
            or payload.get("v") != 1
            or type(payload.get("sequence")) is not int
            or payload["sequence"] <= 0
            or not isinstance(payload.get("id"), str)
            or not payload["id"]
            or not isinstance(payload.get("scope"), str)
            or not isinstance(checksum, str)
        ):
            raise ValueError
        expected_checksum = hashlib.sha256(_canonical_json(payload)).hexdigest()
        if not hmac.compare_digest(checksum, expected_checksum):
            raise ValueError
        if not hmac.compare_digest(payload["scope"], scope):
            raise ValueError
        return payload["sequence"], payload["id"]
    except (
        binascii.Error,
        UnicodeDecodeError,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
    ) as error:
        raise persistence.BookmarkPersistenceValidationError(
            "分页游标无效或与当前预览、账号或筛选条件不匹配"
        ) from error


def _validate_limit(limit: int) -> None:
    if not 1 <= limit <= 100:
        raise persistence.BookmarkPersistenceValidationError("分页数量必须在 1 到 100 之间")


async def _current_complete_preview(
    session: AsyncSession,
    user_id: str,
    job_id: str,
) -> _CurrentPreview:
    job = await session.scalar(
        select(BookmarkImportJob).where(
            BookmarkImportJob.user_id == user_id,
            BookmarkImportJob.id == job_id,
        )
    )
    if job is None:
        raise persistence.BookmarkPersistenceNotFoundError("书签导入任务不存在")

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
    if row is None or row[1].state != "complete":
        raise persistence.BookmarkPersistenceConflictError("书签解析预览尚未完成")
    return _CurrentPreview(job=job, run=row[1])


async def get_preview_summary(
    session: AsyncSession,
    user_id: str,
    job_id: str,
) -> BookmarkPreviewSummaryResponse:
    preview = await _current_complete_preview(session, user_id, job_id)
    occurrence_row = (
        await session.execute(
            select(
                func.sum(
                    case(
                        (BookmarkStagingOccurrence.validation_status == "accepted", 1),
                        else_=0,
                    )
                ),
                func.sum(
                    case(
                        (BookmarkStagingOccurrence.validation_status == "invalid", 1),
                        else_=0,
                    )
                ),
                func.sum(
                    case(
                        (BookmarkStagingOccurrence.validation_status == "unsupported", 1),
                        else_=0,
                    )
                ),
            )
            .select_from(BookmarkStagingOccurrence)
            .where(
                BookmarkStagingOccurrence.user_id == user_id,
                BookmarkStagingOccurrence.run_id == preview.run.id,
            )
        )
    ).one()
    accepted_count, invalid_count, unsupported_count = (int(value or 0) for value in occurrence_row)
    candidate_row = (
        await session.execute(
            select(
                func.count(BookmarkStagingCandidate.id),
                func.sum(
                    case(
                        (BookmarkStagingCandidate.proposed_action == "create", 1),
                        else_=0,
                    )
                ),
                func.sum(
                    case(
                        (BookmarkStagingCandidate.proposed_action == "skip_existing", 1),
                        else_=0,
                    )
                ),
                func.sum(
                    case(
                        (BookmarkStagingCandidate.proposed_action == "merge_missing_metadata", 1),
                        else_=0,
                    )
                ),
                func.sum(
                    case(
                        (BookmarkStagingCandidate.proposed_action == "reject", 1),
                        else_=0,
                    )
                ),
                func.sum(
                    case(
                        (BookmarkStagingCandidate.proposed_action == "needs_review", 1),
                        else_=0,
                    )
                ),
                func.sum(
                    case(
                        (
                            BookmarkStagingCandidate.fetch_policy == "export_metadata_only",
                            1,
                        ),
                        else_=0,
                    )
                ),
                func.sum(
                    case(
                        (BookmarkStagingCandidate.has_sensitive_url.is_(True), 1),
                        else_=0,
                    )
                ),
            )
            .select_from(BookmarkStagingCandidate)
            .where(
                BookmarkStagingCandidate.user_id == user_id,
                BookmarkStagingCandidate.run_id == preview.run.id,
            )
        )
    ).one()
    (
        unique_candidate_count,
        create_count,
        skip_existing_count,
        merge_missing_metadata_count,
        reject_count,
        needs_review_count,
        metadata_only_candidate_count,
        sensitive_candidate_count,
    ) = (int(value or 0) for value in candidate_row)
    return BookmarkPreviewSummaryResponse(
        job_id=preview.job.id,
        run_id=preview.run.id,
        job_version=preview.job.version,
        preview_version=preview.job.preview_version,
        source_sequence_count=preview.run.source_sequence_count,
        folder_count=preview.run.folder_count,
        occurrence_count=preview.run.occurrence_count,
        candidate_count=preview.run.candidate_count,
        occurrence_counts=BookmarkPreviewOccurrenceCounts(
            accepted=accepted_count,
            invalid=invalid_count,
            unsupported=unsupported_count,
        ),
        duplicate_occurrence_count=max(0, accepted_count - unique_candidate_count),
        candidate_action_counts=BookmarkPreviewCandidateActionCounts(
            create=create_count,
            skip_existing=skip_existing_count,
            merge_missing_metadata=merge_missing_metadata_count,
            reject=reject_count,
            needs_review=needs_review_count,
        ),
        metadata_only_candidate_count=metadata_only_candidate_count,
        sensitive_candidate_count=sensitive_candidate_count,
    )


async def list_preview_folders(
    session: AsyncSession,
    user_id: str,
    job_id: str,
    *,
    parent_id: str | None,
    cursor: str | None,
    limit: int,
) -> BookmarkPreviewFolderPageResponse:
    _validate_limit(limit)
    preview = await _current_complete_preview(session, user_id, job_id)
    scope = _cursor_scope(
        user_id=user_id,
        job_id=job_id,
        run_id=preview.run.id,
        job_version=preview.job.version,
        preview_version=preview.job.preview_version,
        endpoint="folders",
        filters={"parent_id": parent_id},
    )
    conditions = [
        BookmarkStagingFolder.user_id == user_id,
        BookmarkStagingFolder.run_id == preview.run.id,
    ]
    if parent_id is not None:
        conditions.append(BookmarkStagingFolder.parent_id == parent_id)
    if cursor is not None:
        sequence, item_id = _decode_cursor(cursor, scope=scope)
        conditions.append(
            tuple_(BookmarkStagingFolder.source_sequence, BookmarkStagingFolder.id)
            > tuple_(sequence, item_id)
        )
    rows = list(
        (
            await session.scalars(
                select(BookmarkStagingFolder)
                .where(*conditions)
                .order_by(BookmarkStagingFolder.source_sequence, BookmarkStagingFolder.id)
                .limit(limit + 1)
            )
        ).all()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [
        BookmarkPreviewFolderResponse(
            id=row.id,
            parent_id=row.parent_id,
            source_sequence=row.source_sequence,
            source_order=row.source_order,
            depth=row.depth,
            title=row.title,
            display_path=json.loads(row.display_path),
        )
        for row in rows
    ]
    next_cursor = (
        _encode_cursor(
            sequence=rows[-1].source_sequence,
            item_id=rows[-1].id,
            scope=scope,
        )
        if has_more
        else None
    )
    return BookmarkPreviewFolderPageResponse(items=items, next_cursor=next_cursor)


async def list_preview_candidates(
    session: AsyncSession,
    user_id: str,
    job_id: str,
    *,
    proposed_action: ProposedAction | None,
    cursor: str | None,
    limit: int,
) -> BookmarkPreviewCandidatePageResponse:
    _validate_limit(limit)
    preview = await _current_complete_preview(session, user_id, job_id)
    scope = _cursor_scope(
        user_id=user_id,
        job_id=job_id,
        run_id=preview.run.id,
        job_version=preview.job.version,
        preview_version=preview.job.preview_version,
        endpoint="candidates",
        filters={"proposed_action": proposed_action},
    )
    conditions = [
        BookmarkStagingCandidate.user_id == user_id,
        BookmarkStagingCandidate.run_id == preview.run.id,
    ]
    if proposed_action is not None:
        conditions.append(BookmarkStagingCandidate.proposed_action == proposed_action)
    if cursor is not None:
        sequence, item_id = _decode_cursor(cursor, scope=scope)
        conditions.append(
            tuple_(
                BookmarkStagingCandidate.first_source_sequence,
                BookmarkStagingCandidate.id,
            )
            > tuple_(sequence, item_id)
        )
    rows = list(
        (
            await session.scalars(
                select(BookmarkStagingCandidate)
                .where(*conditions)
                .order_by(
                    BookmarkStagingCandidate.first_source_sequence,
                    BookmarkStagingCandidate.id,
                )
                .limit(limit + 1)
            )
        ).all()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [
        BookmarkPreviewCandidateResponse(
            id=row.id,
            identity_url=row.identity_url,
            title=row.display_title,
            host=row.host,
            fetch_policy=row.fetch_policy,
            has_sensitive_url=row.has_sensitive_url,
            proposed_action=row.proposed_action,
            occurrence_count=row.occurrence_count,
            first_source_sequence=row.first_source_sequence,
        )
        for row in rows
    ]
    next_cursor = (
        _encode_cursor(
            sequence=rows[-1].first_source_sequence,
            item_id=rows[-1].id,
            scope=scope,
        )
        if has_more
        else None
    )
    return BookmarkPreviewCandidatePageResponse(items=items, next_cursor=next_cursor)


async def list_preview_occurrences(
    session: AsyncSession,
    user_id: str,
    job_id: str,
    *,
    validation_status: ValidationStatus | None,
    folder_id: str | None,
    cursor: str | None,
    limit: int,
) -> BookmarkPreviewOccurrencePageResponse:
    _validate_limit(limit)
    preview = await _current_complete_preview(session, user_id, job_id)
    scope = _cursor_scope(
        user_id=user_id,
        job_id=job_id,
        run_id=preview.run.id,
        job_version=preview.job.version,
        preview_version=preview.job.preview_version,
        endpoint="occurrences",
        filters={"validation_status": validation_status, "folder_id": folder_id},
    )
    conditions = [
        BookmarkStagingOccurrence.user_id == user_id,
        BookmarkStagingOccurrence.run_id == preview.run.id,
    ]
    if validation_status is not None:
        conditions.append(BookmarkStagingOccurrence.validation_status == validation_status)
    if folder_id is not None:
        conditions.append(BookmarkStagingOccurrence.folder_id == folder_id)
    if cursor is not None:
        sequence, item_id = _decode_cursor(cursor, scope=scope)
        conditions.append(
            tuple_(
                BookmarkStagingOccurrence.source_sequence,
                BookmarkStagingOccurrence.id,
            )
            > tuple_(sequence, item_id)
        )
    rows = list(
        (
            await session.scalars(
                select(BookmarkStagingOccurrence)
                .where(*conditions)
                .order_by(
                    BookmarkStagingOccurrence.source_sequence,
                    BookmarkStagingOccurrence.id,
                )
                .limit(limit + 1)
            )
        ).all()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [
        BookmarkPreviewOccurrenceResponse(
            id=row.id,
            folder_id=row.folder_id,
            source_sequence=row.source_sequence,
            source_order=row.source_order,
            title=row.raw_title,
            url=row.raw_url,
            add_date=row.add_date,
            last_modified=row.last_modified,
            validation_status=row.validation_status,
            fetch_policy=row.fetch_policy,
            reason_code=row.reason_code,
            has_sensitive_url=row.has_sensitive_url,
        )
        for row in rows
    ]
    next_cursor = (
        _encode_cursor(
            sequence=rows[-1].source_sequence,
            item_id=rows[-1].id,
            scope=scope,
        )
        if has_more
        else None
    )
    return BookmarkPreviewOccurrencePageResponse(items=items, next_cursor=next_cursor)


async def apply_import(
    session: AsyncSession,
    user_id: str,
    job_id: str,
    *,
    expected_job_version: int,
) -> tuple[BookmarkImportApplyResponse, tuple[str, ...]]:
    """Write the job's staged candidates into the account's library.

    Account-scoped through ``_current_complete_preview``: a job id belonging to
    another account simply does not resolve.  The optimistic-lock check means a
    preview the user is looking at cannot be applied after it was superseded by
    a re-parse.
    """

    preview = await _current_complete_preview(session, user_id, job_id)
    if preview.job.version != expected_job_version:
        raise persistence.BookmarkPersistenceConflictError("导入任务已被更新，请刷新预览后重试")
    if preview.job.state in {"committing", "cancel_requested"}:
        raise persistence.BookmarkPersistenceConflictError("该导入任务正在处理中")

    outcome = await apply_candidates(session, user_id, preview.run.id)

    # Re-read: apply_candidates committed, so the in-session copy is stale.
    job = await session.scalar(
        select(BookmarkImportJob).where(
            BookmarkImportJob.user_id == user_id,
            BookmarkImportJob.id == job_id,
        )
    )
    if job is not None:
        job.state = "completed_with_errors" if outcome.failed else "completed"
        job.completed_at = utc_now()
        job.version += 1
        await session.commit()

    response = BookmarkImportApplyResponse(
        job_id=job_id,
        state=job.state if job is not None else "completed",
        job_version=job.version if job is not None else expected_job_version,
        **outcome.as_dict(),
    )
    return response, outcome.created_site_ids
