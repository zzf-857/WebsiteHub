from __future__ import annotations

import asyncio
import errno
import logging
import re
from collections.abc import Awaitable
from contextlib import suppress
from typing import Annotated
from urllib.parse import unquote_to_bytes

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    status,
)
from sqlalchemy.exc import DBAPIError

from webhub.auth.dependencies import (
    CurrentIdentityDependency,
    DatabaseSessionDependency,
    SettingsDependency,
    require_trusted_origin,
)
from webhub.bookmarks import intake, persistence, queries, worker
from webhub.bookmarks.admission import (
    BookmarkUploadQuotaExceededError,
    BookmarkUploadRateLimitError,
    BookmarkUploadStorageUnavailableError,
)
from webhub.bookmarks.models import BookmarkFormatError, ParserLimits
from webhub.bookmarks.schemas import (
    BookmarkImportApplyRequest,
    BookmarkImportApplyResponse,
    BookmarkImportStatusResponse,
    BookmarkImportUploadResponse,
    BookmarkPreviewCandidatePageResponse,
    BookmarkPreviewFolderPageResponse,
    BookmarkPreviewOccurrencePageResponse,
    BookmarkPreviewSummaryResponse,
    ProposedAction,
    ValidationStatus,
)
from webhub.bookmarks.uploads import (
    BookmarkUploadTooLargeError,
    StagedBookmarkUpload,
    stage_bookmark_upload,
)
from webhub.ingestion import service as ingestion_service
from webhub.ingestion import worker as ingestion_worker
from webhub.ingestion.enrichment import AnalysisIntent

router = APIRouter(prefix="/bookmark-imports", tags=["bookmark-imports"])
_IMMEDIATE_IMPORTED_ANALYSIS_LIMIT = 8
_LOGGER = logging.getLogger(__name__)

# Keeps a strong reference to every detached parse task: asyncio only holds a
# weak one, so without this a task can be garbage collected mid-parse.
_PARSE_TASKS: set[asyncio.Task[str]] = set()


def _schedule_parse(
    request: Request,
    *,
    user_id: str,
    job_id: str,
    snapshot_id: str,
    expected_job_version: int,
) -> None:
    task = asyncio.create_task(
        worker.run_parse(
            request.app.state.database,
            request.app.state.settings.data_directory,
            user_id=user_id,
            job_id=job_id,
            snapshot_id=snapshot_id,
            expected_job_version=expected_job_version,
        )
    )
    _PARSE_TASKS.add(task)
    task.add_done_callback(_PARSE_TASKS.discard)


JobId = Annotated[str, Path(max_length=128)]
OptionalIdentifier = Annotated[str | None, Query(max_length=128)]
WriteOriginDependency = Annotated[None, Depends(require_trusted_origin)]
IdempotencyKeyHeader = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=16, max_length=512),
]

_SUPPORTED_UPLOAD_MEDIA_TYPES = {
    "application/octet-stream",
    "text/html",
}
_INSUFFICIENT_STORAGE_ERRNOS = {
    errno.ENOSPC,
    getattr(errno, "EDQUOT", errno.ENOSPC),
}
_STORAGE_UNAVAILABLE_MESSAGE = "服务器暂时没有足够的书签导入存储空间"
_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_UPLOAD_REQUEST_BODY = {
    "requestBody": {
        "required": True,
        "content": {
            "text/html": {"schema": {"type": "string", "format": "binary"}},
            "application/octet-stream": {"schema": {"type": "string", "format": "binary"}},
        },
    }
}
_UPLOAD_RESPONSES: dict[int, dict[str, object]] = {
    status.HTTP_200_OK: {
        "model": BookmarkImportUploadResponse,
        "description": "Idempotent replay of an existing upload job",
    },
    status.HTTP_400_BAD_REQUEST: {"description": "Malformed Content-Length"},
    status.HTTP_401_UNAUTHORIZED: {"description": "Authentication required"},
    status.HTTP_403_FORBIDDEN: {"description": "Untrusted request origin"},
    status.HTTP_409_CONFLICT: {"description": "Idempotency or publication conflict"},
    status.HTTP_413_CONTENT_TOO_LARGE: {"description": "File or account upload quota exceeded"},
    status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: {"description": "Unsupported upload media type"},
    status.HTTP_429_TOO_MANY_REQUESTS: {
        "description": "Account or global upload admission limit reached"
    },
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "description": "Invalid bookmark export or request metadata"
    },
    status.HTTP_507_INSUFFICIENT_STORAGE: {"description": "Host storage reserve is unavailable"},
}


async def _call[T](operation: Awaitable[T], *, no_store: bool = False) -> T:
    try:
        return await operation
    except persistence.BookmarkPersistenceError as error:
        headers = {"Cache-Control": "no-store"} if no_store else None
        raise HTTPException(error.status_code, error.message, headers=headers) from error
    except DBAPIError as error:
        if not persistence.is_database_storage_exhausted(error):
            raise
        headers = {"Cache-Control": "no-store"} if no_store else None
        raise HTTPException(
            status.HTTP_507_INSUFFICIENT_STORAGE,
            _STORAGE_UNAVAILABLE_MESSAGE,
            headers=headers,
        ) from error


def _validate_idempotency_key(value: str) -> str:
    candidate = value.strip()
    if not 16 <= len(candidate) <= 512:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Idempotency-Key 长度必须在 16 到 512 之间",
        )
    return candidate


def _validate_content_type(request: Request) -> None:
    raw_content_type = request.headers.get("content-type")
    media_type = raw_content_type.split(";", 1)[0].strip().casefold() if raw_content_type else ""
    if media_type not in _SUPPORTED_UPLOAD_MEDIA_TYPES:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "不支持的书签上传媒体类型")


def _validate_content_length(request: Request, *, maximum: int) -> int | None:
    raw_content_length = request.headers.get("content-length")
    if raw_content_length is None:
        return None
    candidate = raw_content_length.strip()
    if re.fullmatch(r"[0-9]+", candidate) is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Content-Length 格式无效")
    if int(candidate) > maximum:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"书签文件不能超过 {maximum} 字节",
        )
    return int(candidate)


def _decode_filename(request: Request) -> str | None:
    encoded = request.headers.get("x-bookmark-filename")
    if encoded is None:
        return None
    if _INVALID_PERCENT_ESCAPE.search(encoded):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "X-Bookmark-Filename 包含无效的百分号编码",
        )
    try:
        return unquote_to_bytes(encoded).decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "X-Bookmark-Filename 必须是百分号编码的 UTF-8",
        ) from error


@router.post(
    "",
    response_model=BookmarkImportUploadResponse,
    status_code=status.HTTP_201_CREATED,
    responses=_UPLOAD_RESPONSES,
    openapi_extra=_UPLOAD_REQUEST_BODY,
)
async def upload_bookmarks(
    request: Request,
    response: Response,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    settings: SettingsDependency,
    _: WriteOriginDependency,
    idempotency_key: IdempotencyKeyHeader,
) -> BookmarkImportUploadResponse:
    request_key = _validate_idempotency_key(idempotency_key)
    _validate_content_type(request)
    maximum = ParserLimits().max_file_bytes
    content_length = _validate_content_length(request, maximum=maximum)
    original_filename = _decode_filename(request)
    account_id = identity.user.id

    await session.rollback()
    staged_upload: StagedBookmarkUpload | None = None
    try:
        admission_manager = request.app.state.bookmark_upload_admission
        async with admission_manager.admit(
            account_id,
            declared_size_bytes=content_length,
        ) as upload_admission:
            staged_upload = await stage_bookmark_upload(
                upload_admission.guard_chunks(request.stream()),
                data_directory=settings.data_directory,
                account_id=account_id,
                original_filename=original_filename,
                max_file_bytes=maximum,
            )
            source_sha256 = staged_upload.source_sha256
            import_job = await _call(
                intake.intake_bookmark_upload(
                    session,
                    data_directory=settings.data_directory,
                    account_id=account_id,
                    staged_upload=staged_upload,
                    idempotency_key=request_key,
                )
            )
            staged_upload = None
            same_source_jobs = await _call(
                persistence.find_same_source(
                    session,
                    account_id,
                    source_sha256,
                )
            )

            response.status_code = (
                status.HTTP_200_OK if import_job.replayed else status.HTTP_201_CREATED
            )
            response.headers["Location"] = f"/api/bookmark-imports/{import_job.job_id}"

            # Nothing else in the process drives queued_parse forward, so kick
            # the parse off here.  Detached on purpose: the caller gets a job id
            # immediately and polls status, exactly as it would with a real
            # worker.  A replayed upload already has its run.
            if not import_job.replayed and import_job.state == "queued_parse":
                _schedule_parse(
                    request,
                    user_id=account_id,
                    job_id=import_job.job_id,
                    snapshot_id=import_job.snapshot_id,
                    expected_job_version=import_job.job_version,
                )

            return BookmarkImportUploadResponse(
                job_id=import_job.job_id,
                state=import_job.state,
                job_version=import_job.job_version,
                replayed=import_job.replayed,
                same_source_warning=(
                    not import_job.replayed
                    and any(item.snapshot_id != import_job.snapshot_id for item in same_source_jobs)
                ),
            )
    except BookmarkUploadRateLimitError as error:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "书签上传请求过于频繁，请稍后重试",
            headers={"Retry-After": str(error.retry_after)},
        ) from error
    except BookmarkUploadQuotaExceededError as error:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            "当前账号的书签导入暂存空间已用完",
        ) from error
    except BookmarkUploadStorageUnavailableError as error:
        raise HTTPException(
            status.HTTP_507_INSUFFICIENT_STORAGE,
            _STORAGE_UNAVAILABLE_MESSAGE,
        ) from error
    except BookmarkUploadTooLargeError as error:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, str(error)) from error
    except BookmarkFormatError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
    except OSError as error:
        if error.errno not in _INSUFFICIENT_STORAGE_ERRNOS:
            raise
        raise HTTPException(
            status.HTTP_507_INSUFFICIENT_STORAGE,
            _STORAGE_UNAVAILABLE_MESSAGE,
        ) from error
    finally:
        if staged_upload is not None:
            with suppress(Exception):
                intake.discard_staged_upload(
                    staged_upload,
                    data_directory=settings.data_directory,
                    account_id=account_id,
                )
        with suppress(Exception):
            await session.rollback()


@router.get("/{job_id}", response_model=BookmarkImportStatusResponse)
async def import_status(
    job_id: JobId,
    response: Response,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
) -> BookmarkImportStatusResponse:
    response.headers["Cache-Control"] = "no-store"
    return await _call(
        queries.get_import_status(session, identity.user.id, job_id),
        no_store=True,
    )


@router.get("/{job_id}/preview", response_model=BookmarkPreviewSummaryResponse)
async def preview_summary(
    job_id: JobId,
    response: Response,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
) -> BookmarkPreviewSummaryResponse:
    response.headers["Cache-Control"] = "no-store"
    return await _call(
        queries.get_preview_summary(session, identity.user.id, job_id),
        no_store=True,
    )


@router.get(
    "/{job_id}/preview/folders",
    response_model=BookmarkPreviewFolderPageResponse,
)
async def preview_folders(
    job_id: JobId,
    response: Response,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    parent_id: OptionalIdentifier = None,
    cursor: Annotated[str | None, Query(max_length=2_048)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> BookmarkPreviewFolderPageResponse:
    response.headers["Cache-Control"] = "no-store"
    return await _call(
        queries.list_preview_folders(
            session,
            identity.user.id,
            job_id,
            parent_id=parent_id,
            cursor=cursor,
            limit=limit,
        ),
        no_store=True,
    )


@router.get(
    "/{job_id}/preview/candidates",
    response_model=BookmarkPreviewCandidatePageResponse,
)
async def preview_candidates(
    job_id: JobId,
    response: Response,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    proposed_action: ProposedAction | None = None,
    cursor: Annotated[str | None, Query(max_length=2_048)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> BookmarkPreviewCandidatePageResponse:
    response.headers["Cache-Control"] = "no-store"
    return await _call(
        queries.list_preview_candidates(
            session,
            identity.user.id,
            job_id,
            proposed_action=proposed_action,
            cursor=cursor,
            limit=limit,
        ),
        no_store=True,
    )


@router.get(
    "/{job_id}/preview/occurrences",
    response_model=BookmarkPreviewOccurrencePageResponse,
)
async def preview_occurrences(
    job_id: JobId,
    response: Response,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    validation_status: ValidationStatus | None = None,
    folder_id: OptionalIdentifier = None,
    cursor: Annotated[str | None, Query(max_length=2_048)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> BookmarkPreviewOccurrencePageResponse:
    response.headers["Cache-Control"] = "no-store"
    return await _call(
        queries.list_preview_occurrences(
            session,
            identity.user.id,
            job_id,
            validation_status=validation_status,
            folder_id=folder_id,
            cursor=cursor,
            limit=limit,
        ),
        no_store=True,
    )


@router.post("/{job_id}/apply", response_model=BookmarkImportApplyResponse)
async def apply_import(
    job_id: str,
    payload: BookmarkImportApplyRequest,
    request: Request,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
) -> BookmarkImportApplyResponse:
    """Write the job's staged candidates into the account's library.

    Nothing before this endpoint ever created a ``Site``: upload, parse and
    preview are all read-only from the library's point of view.  That is what
    makes "确认前网址库无变化" true by construction rather than by discipline.
    """

    response = await _call(
        queries.apply_import(
            session,
            identity.user.id,
            job_id,
            expected_job_version=payload.expected_job_version,
        )
    )
    if response.created > 0:
        try:
            immediate_site_ids = await ingestion_service.recent_not_analyzed_site_ids(
                session,
                str(identity.user.id),
                limit=_IMMEDIATE_IMPORTED_ANALYSIS_LIMIT,
            )
        except Exception:  # noqa: BLE001 - import already committed; background recovery remains
            _LOGGER.warning("could not prioritize freshly imported site analysis", exc_info=True)
        else:
            ingestion_worker.schedule_analysis(
                request.app.state.database,
                user_id=str(identity.user.id),
                site_ids=tuple(immediate_site_ids),
                priority=True,
                intent=AnalysisIntent.SITE_ENRICHMENT,
                bulk=True,
            )
    ingestion_worker.ensure_auto_backfill(
        request.app.state.database,
        user_id=str(identity.user.id),
        # If a previous sweep is on its final empty discovery query, make it
        # perform one more pass after this commit rather than losing every new
        # bookmark until the next library request wakes it.
        rescan_if_running=response.created > 0,
    )
    return response
