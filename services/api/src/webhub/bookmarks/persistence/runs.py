from __future__ import annotations

import json
import re

from sqlalchemy import and_, func, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.db.models import (
    BookmarkImportCurrentRun,
    BookmarkImportJob,
    BookmarkImportRun,
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
    BookmarkPersistenceConflictError,
    BookmarkPersistenceError,
    BookmarkPersistenceValidationError,
    ParsePreviewSummary,
    ParseRunResult,
    _is_database_busy,
    _key_hash,
    _owned_run,
    _sha256,
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
async def begin_parse_run(
    session: AsyncSession,
    user_id: str,
    job_id: str,
    *,
    expected_job_version: int,
    idempotency_key: str,
) -> ParseRunResult:
    job = await _common._owned_job(session, user_id, job_id)
    key_hash = _key_hash(idempotency_key, field="解析运行幂等键")
    existing = await _common._parse_run_replay(session, user_id, job_id, key_hash)
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
        replay = await _common._parse_run_replay(session, user_id, job_id, key_hash)
        if replay is not None:
            return replay
        raise BookmarkPersistenceConflictError("书签导入任务已被修改，请刷新后重试")

    input_hash = _sha256(
        json.dumps(
            {
                "source_sha256": snapshot.source_sha256,
                "parser_version": _common.PARSER_VERSION,
                "normalizer_version": _common.NORMALIZER_VERSION,
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
        parser_version=_common.PARSER_VERSION,
        normalizer_version=_common.NORMALIZER_VERSION,
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
        replay = await _common._parse_run_replay(session, user_id, job_id, key_hash)
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
    job = await _common._owned_job(session, user_id, job_id)
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
