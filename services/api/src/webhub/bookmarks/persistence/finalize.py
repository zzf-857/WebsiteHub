from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.db.models import (
    BookmarkImportCurrentRun,
    BookmarkImportJob,
    BookmarkImportRun,
    BookmarkImportSnapshot,
    utc_now,
)

# 下面这几个名字用 ``_common.X`` 限定访问，而不是 ``from ._common import X``：
# ``from ... import`` 在导入时就把值绑进本模块命名空间，测试再 patch 一处就到不了
# 其余模块。限定访问是每次调用现取，patch ``persistence._common`` 上的那一份即可
# 覆盖全包——这是拆包前单文件天然具备的性质，不该因为拆分而丢掉。
from . import _common, staging
from ._common import (
    BookmarkPersistenceConflictError,
    BookmarkPersistenceError,
    BookmarkPersistenceValidationError,
    ParseCompletion,
    ParsePreviewSummary,
    _assert_run_versions,
    _is_database_busy,
    _owned_run,
    _parse_completion_hash,
    _validate_digest,
)
from .runs import (
    _completed_parse_preview_replay,
    _release_parse_run_seal,
)
from .staging import (
    _rebuild_candidate_projections,
    _staged_completion,
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

    job = await _common._owned_job(session, user_id, job_id)
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
        await staging._validate_complete_staging(session, user_id, run_id, completion)
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
