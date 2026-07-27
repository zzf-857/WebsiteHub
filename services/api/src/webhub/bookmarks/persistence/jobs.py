from __future__ import annotations

import re

from sqlalchemy import and_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.bookmarks.models import (
    ParserLimits,
)
from webhub.db.models import (
    BookmarkImportJob,
    BookmarkImportSnapshot,
    new_id,
    utc_now,
)

# 下面这几个名字用 ``_common.X`` 限定访问，而不是 ``from ._common import X``：
# ``from ... import`` 在导入时就把值绑进本模块命名空间，测试再 patch 一处就到不了
# 其余模块。限定访问是每次调用现取，patch ``persistence._common`` 上的那一份即可
# 覆盖全包——这是拆包前单文件天然具备的性质，不该因为拆分而丢掉。
from . import _common
from ._common import (
    SKILL_VERSION,
    BookmarkPersistenceConflictError,
    BookmarkPersistenceValidationError,
    ImportJobResult,
    SameSourceResult,
    _display_filename,
    _is_database_busy,
    _job_result,
    _key_hash,
    _owned_import,
    _validate_digest,
)


async def create_import(
    session: AsyncSession,
    user_id: str,
    *,
    source_sha256: str,
    source_size_bytes: int,
    original_filename: str | None,
    idempotency_key: str,
    detected_encoding: str | None = None,
    ready_for_parse: bool = True,
) -> ImportJobResult:
    source_hash = _validate_digest(source_sha256, field="源文件摘要")
    if not 0 < source_size_bytes <= ParserLimits().max_file_bytes:
        raise BookmarkPersistenceValidationError("源文件大小超出书签导入限制")
    normalized_encoding = detected_encoding.strip().casefold() if detected_encoding else None
    if normalized_encoding is not None and (
        len(normalized_encoding) > 40
        or re.fullmatch(r"[a-z0-9._-]+", normalized_encoding) is None
    ):
        raise BookmarkPersistenceValidationError("书签文件编码标识不合法")
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
        detected_encoding=normalized_encoding,
        request_idempotency_key_hash=request_key_hash,
        created_at=now,
    )
    job = BookmarkImportJob(
        id=job_id,
        user_id=user_id,
        snapshot_id=snapshot_id,
        state="queued_parse" if ready_for_parse else "receiving",
        parser_version=_common.PARSER_VERSION,
        normalizer_version=_common.NORMALIZER_VERSION,
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


async def queue_import_for_parse(
    session: AsyncSession,
    user_id: str,
    job_id: str,
    *,
    expected_job_version: int,
) -> ImportJobResult:
    if (
        isinstance(expected_job_version, bool)
        or not isinstance(expected_job_version, int)
        or expected_job_version <= 0
    ):
        raise BookmarkPersistenceValidationError("任务版本必须为正整数")
    snapshot, job = await _owned_import(session, user_id, job_id)
    if job.state != "receiving":
        return _job_result(snapshot, job, replayed=True)
    if job.version != expected_job_version:
        raise BookmarkPersistenceConflictError("书签导入任务已被修改，请刷新后重试")

    try:
        queued = await session.execute(
            update(BookmarkImportJob)
            .where(
                BookmarkImportJob.user_id == user_id,
                BookmarkImportJob.id == job_id,
                BookmarkImportJob.state == "receiving",
                BookmarkImportJob.version == expected_job_version,
            )
            .values(
                state="queued_parse",
                version=BookmarkImportJob.version + 1,
                updated_at=utc_now(),
            )
        )
        if queued.rowcount == 1:
            await session.commit()
            await session.refresh(job)
            return _job_result(snapshot, job, replayed=False)
        await session.rollback()
    except OperationalError as error:
        await session.rollback()
        if not _is_database_busy(error):
            raise

    replay_snapshot, replay_job = await _owned_import(session, user_id, job_id)
    if replay_job.state != "receiving":
        return _job_result(replay_snapshot, replay_job, replayed=True)
    raise BookmarkPersistenceConflictError("书签导入任务已被修改，请刷新后重试")


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
