from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, case, delete, func, literal, select, union_all, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.bookmarks.models import (
    NormalizationStatus,
)
from webhub.bookmarks.normalization import normalize_bookmark_url
from webhub.bookmarks.privacy import sensitive_url_keys
from webhub.db.models import (
    BookmarkImportCheckpoint,
    BookmarkImportCurrentRun,
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

# 下面这几个名字用 ``_common.X`` 限定访问，而不是 ``from ._common import X``：
# ``from ... import`` 在导入时就把值绑进本模块命名空间，测试再 patch 一处就到不了
# 其余模块。限定访问是每次调用现取，patch ``persistence._common`` 上的那一份即可
# 覆盖全包——这是拆包前单文件天然具备的性质，不该因为拆分而丢掉。
from . import _common
from ._common import (
    BookmarkPersistenceConflictError,
    BookmarkPersistenceNotFoundError,
    BookmarkPersistenceValidationError,
    ParseCompletion,
    ParsePreviewSummary,
    _event_batch_hash,
    _sha256,
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
            select(
                func.count(func.distinct(BookmarkStagingCandidateOccurrence.candidate_id))
            ).where(
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
            raise BookmarkPersistenceValidationError("occurrence 与 candidate identity 投影不一致")


async def _staged_completion(
    session: AsyncSession,
    user_id: str,
    job_id: str,
    run_id: str,
) -> ParseCompletion:
    job = await _common._owned_job(session, user_id, job_id)
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


async def get_current_preview_summary(
    session: AsyncSession,
    user_id: str,
    job_id: str,
) -> ParsePreviewSummary:
    job = await _common._owned_job(session, user_id, job_id)
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
