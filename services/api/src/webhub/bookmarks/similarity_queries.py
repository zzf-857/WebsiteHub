from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import and_, case, func, literal, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.bookmarks import persistence, queries
from webhub.bookmarks.schemas import (
    BookmarkSimilarityCanonicalResponse,
    BookmarkSimilarityClusterPageResponse,
    BookmarkSimilarityClusterResponse,
    BookmarkSimilarityDecisionCounts,
    BookmarkSimilarityDecisionResponse,
    BookmarkSimilarityMemberPageResponse,
    BookmarkSimilarityMemberResponse,
    SimilarityDecision,
    SimilarityDecisionFilter,
)
from webhub.bookmarks.similarity import safe_display_url
from webhub.db.models import (
    BookmarkImportJob,
    BookmarkSimilarityCluster,
    BookmarkSimilarityClusterMember,
    BookmarkSimilarityDecision,
    BookmarkSimilarityDecisionState,
    BookmarkStagingCandidate,
    utc_now,
)


@dataclass(frozen=True, slots=True)
class SimilarityStatistics:
    decision_version: int
    cluster_count: int
    candidate_count: int
    decision_counts: BookmarkSimilarityDecisionCounts
    selected_merge_reduction_count: int
    projected_create_count: int


def _decision_join() -> object:
    return and_(
        BookmarkSimilarityDecision.user_id == BookmarkSimilarityCluster.user_id,
        BookmarkSimilarityDecision.run_id == BookmarkSimilarityCluster.run_id,
        BookmarkSimilarityDecision.cluster_id == BookmarkSimilarityCluster.id,
    )


async def _decision_version(
    session: AsyncSession,
    *,
    user_id: str,
    run_id: str,
    require_state: bool,
) -> int:
    version = await session.scalar(
        select(BookmarkSimilarityDecisionState.version).where(
            BookmarkSimilarityDecisionState.user_id == user_id,
            BookmarkSimilarityDecisionState.run_id == run_id,
        )
    )
    if version is not None:
        return int(version)
    if require_state:
        raise persistence.BookmarkPersistenceConflictError(
            "相似书签决策快照缺失，请重新解析书签文件"
        )
    # Runs published before Q29 have no similarity projection. They remain
    # applicable and expose an empty, version-1 decision surface.
    return 1


async def similarity_statistics(
    session: AsyncSession,
    user_id: str,
    run_id: str,
    *,
    base_create_count: int,
) -> SimilarityStatistics:
    row = (
        await session.execute(
            select(
                func.count(BookmarkSimilarityCluster.id),
                func.sum(BookmarkSimilarityCluster.candidate_count),
                func.sum(
                    case(
                        (BookmarkSimilarityDecision.cluster_id.is_(None), 1),
                        else_=0,
                    )
                ),
                func.sum(
                    case(
                        (BookmarkSimilarityDecision.decision == "merge_to_homepage", 1),
                        else_=0,
                    )
                ),
                func.sum(
                    case(
                        (BookmarkSimilarityDecision.decision == "keep_originals", 1),
                        else_=0,
                    )
                ),
                func.sum(
                    case(
                        (
                            BookmarkSimilarityDecision.decision == "merge_to_homepage",
                            BookmarkSimilarityCluster.keep_original_create_count
                            - BookmarkSimilarityCluster.merge_create_count,
                        ),
                        else_=0,
                    )
                ),
            )
            .select_from(BookmarkSimilarityCluster)
            .outerjoin(BookmarkSimilarityDecision, _decision_join())
            .where(
                BookmarkSimilarityCluster.user_id == user_id,
                BookmarkSimilarityCluster.run_id == run_id,
            )
        )
    ).one()
    (
        cluster_count,
        candidate_count,
        unresolved_count,
        merge_count,
        keep_count,
        reduction_count,
    ) = (int(value or 0) for value in row)
    version = await _decision_version(
        session,
        user_id=user_id,
        run_id=run_id,
        require_state=cluster_count > 0,
    )
    return SimilarityStatistics(
        decision_version=version,
        cluster_count=cluster_count,
        candidate_count=candidate_count,
        decision_counts=BookmarkSimilarityDecisionCounts(
            unresolved=unresolved_count,
            merge_to_homepage=merge_count,
            keep_originals=keep_count,
        ),
        selected_merge_reduction_count=reduction_count,
        projected_create_count=max(0, base_create_count - reduction_count),
    )


def _reason_codes(value: str) -> list[str]:
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return []
    if not isinstance(decoded, list):
        return []
    return [item for item in decoded if isinstance(item, str) and item][:8]


def _member_response(row: object) -> BookmarkSimilarityMemberResponse:
    return BookmarkSimilarityMemberResponse(
        candidate_id=row.candidate_id,
        title=row.title,
        display_url=safe_display_url(row.identity_url),
        occurrence_count=row.occurrence_count,
        first_source_sequence=row.first_source_sequence,
        is_canonical=bool(row.is_canonical),
    )


async def _sample_members(
    session: AsyncSession,
    user_id: str,
    run_id: str,
    cluster_ids: list[str],
) -> dict[str, list[BookmarkSimilarityMemberResponse]]:
    if not cluster_ids:
        return {}
    ranked = (
        select(
            BookmarkSimilarityClusterMember.cluster_id.label("cluster_id"),
            BookmarkSimilarityClusterMember.candidate_id.label("candidate_id"),
            BookmarkSimilarityClusterMember.first_source_sequence.label(
                "first_source_sequence"
            ),
            BookmarkSimilarityClusterMember.is_canonical.label("is_canonical"),
            BookmarkStagingCandidate.display_title.label("title"),
            BookmarkStagingCandidate.identity_url.label("identity_url"),
            BookmarkStagingCandidate.occurrence_count.label("occurrence_count"),
            func.row_number()
            .over(
                partition_by=BookmarkSimilarityClusterMember.cluster_id,
                order_by=(
                    BookmarkSimilarityClusterMember.first_source_sequence,
                    BookmarkSimilarityClusterMember.candidate_id,
                ),
            )
            .label("member_rank"),
        )
        .join(
            BookmarkStagingCandidate,
            and_(
                BookmarkStagingCandidate.user_id
                == BookmarkSimilarityClusterMember.user_id,
                BookmarkStagingCandidate.run_id == BookmarkSimilarityClusterMember.run_id,
                BookmarkStagingCandidate.id == BookmarkSimilarityClusterMember.candidate_id,
            ),
        )
        .where(
            BookmarkSimilarityClusterMember.user_id == user_id,
            BookmarkSimilarityClusterMember.run_id == run_id,
            BookmarkSimilarityClusterMember.cluster_id.in_(cluster_ids),
        )
        .subquery()
    )
    rows = (
        await session.execute(
            select(ranked)
            .where(ranked.c.member_rank <= 3)
            .order_by(
                ranked.c.cluster_id,
                ranked.c.first_source_sequence,
                ranked.c.candidate_id,
            )
        )
    ).all()
    result: dict[str, list[BookmarkSimilarityMemberResponse]] = {}
    for row in rows:
        result.setdefault(row.cluster_id, []).append(_member_response(row))
    return result


async def list_similarity_clusters(
    session: AsyncSession,
    user_id: str,
    job_id: str,
    *,
    decision: SimilarityDecisionFilter | None,
    cursor: str | None,
    page: int | None,
    limit: int,
) -> BookmarkSimilarityClusterPageResponse:
    queries._validate_limit(limit)
    if cursor is not None and page is not None:
        raise persistence.BookmarkPersistenceValidationError(
            "分页游标和页码不能同时使用"
        )
    if page is not None and page < 1:
        raise persistence.BookmarkPersistenceValidationError("相似书签页码必须是正整数")
    preview = await queries._current_complete_preview(session, user_id, job_id)
    cluster_count = int(
        await session.scalar(
            select(func.count())
            .select_from(BookmarkSimilarityCluster)
            .where(
                BookmarkSimilarityCluster.user_id == user_id,
                BookmarkSimilarityCluster.run_id == preview.run.id,
            )
        )
        or 0
    )
    decision_version = await _decision_version(
        session,
        user_id=user_id,
        run_id=preview.run.id,
        require_state=cluster_count > 0,
    )
    scope = queries._cursor_scope(
        user_id=user_id,
        job_id=job_id,
        run_id=preview.run.id,
        job_version=preview.job.version,
        preview_version=preview.job.preview_version,
        endpoint="similarity_clusters",
        filters={
            "decision": decision,
            "decision_version": str(decision_version) if decision is not None else None,
        },
    )
    conditions = [
        BookmarkSimilarityCluster.user_id == user_id,
        BookmarkSimilarityCluster.run_id == preview.run.id,
    ]
    if decision == "unresolved":
        conditions.append(BookmarkSimilarityDecision.cluster_id.is_(None))
    elif decision is not None:
        conditions.append(BookmarkSimilarityDecision.decision == decision)
    total_count = cluster_count
    if decision is not None:
        total_count = int(
            await session.scalar(
                select(func.count())
                .select_from(BookmarkSimilarityCluster)
                .outerjoin(BookmarkSimilarityDecision, _decision_join())
                .where(*conditions)
            )
            or 0
        )
    total_pages = (total_count + limit - 1) // limit
    current_page = page or 1
    offset = 0
    if cursor is not None:
        sequence, item_id = queries._decode_cursor(cursor, scope=scope)
        preceding_count = int(
            await session.scalar(
                select(func.count())
                .select_from(BookmarkSimilarityCluster)
                .outerjoin(BookmarkSimilarityDecision, _decision_join())
                .where(
                    *conditions,
                    (BookmarkSimilarityCluster.first_source_sequence < sequence)
                    | (
                        (BookmarkSimilarityCluster.first_source_sequence == sequence)
                        & (BookmarkSimilarityCluster.id <= item_id)
                    ),
                )
            )
            or 0
        )
        current_page = preceding_count // limit + 1
        conditions.append(
            (BookmarkSimilarityCluster.first_source_sequence > sequence)
            | (
                (BookmarkSimilarityCluster.first_source_sequence == sequence)
                & (BookmarkSimilarityCluster.id > item_id)
            )
        )
    else:
        if current_page > max(total_pages, 1):
            raise persistence.BookmarkPersistenceValidationError(
                "相似书签页码超出范围"
            )
        offset = (current_page - 1) * limit
    statement = (
        select(BookmarkSimilarityCluster, BookmarkSimilarityDecision.decision)
        .outerjoin(BookmarkSimilarityDecision, _decision_join())
        .where(*conditions)
        .order_by(
            BookmarkSimilarityCluster.first_source_sequence,
            BookmarkSimilarityCluster.id,
        )
        .limit(limit + 1)
    )
    if cursor is None:
        statement = statement.offset(offset)
    rows = (
        await session.execute(
            statement
        )
    ).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    samples = await _sample_members(
        session,
        user_id,
        preview.run.id,
        [cluster.id for cluster, _ in rows],
    )
    items = [
        BookmarkSimilarityClusterResponse(
            id=cluster.id,
            display_host=cluster.display_host,
            confidence=cluster.confidence,
            reason_codes=_reason_codes(cluster.reason_codes_json),
            candidate_count=cluster.candidate_count,
            occurrence_count=cluster.occurrence_count,
            first_source_sequence=cluster.first_source_sequence,
            decision=current_decision,
            canonical=BookmarkSimilarityCanonicalResponse(
                candidate_id=cluster.canonical_candidate_id,
                url=cluster.canonical_url,
                title=cluster.canonical_title,
                source=cluster.canonical_source,
            ),
            sample_members=samples.get(cluster.id, []),
            has_more_members=cluster.candidate_count > len(samples.get(cluster.id, [])),
        )
        for cluster, current_decision in rows
    ]
    next_cursor = (
        queries._encode_cursor(
            sequence=rows[-1][0].first_source_sequence,
            item_id=rows[-1][0].id,
            scope=scope,
        )
        if has_more
        else None
    )
    return BookmarkSimilarityClusterPageResponse(
        items=items,
        next_cursor=next_cursor,
        page=current_page,
        page_size=limit,
        total_count=total_count,
        total_pages=total_pages,
        decision_version=decision_version,
    )


async def list_similarity_members(
    session: AsyncSession,
    user_id: str,
    job_id: str,
    cluster_id: str,
    *,
    cursor: str | None,
    limit: int,
) -> BookmarkSimilarityMemberPageResponse:
    queries._validate_limit(limit)
    preview = await queries._current_complete_preview(session, user_id, job_id)
    cluster = await session.scalar(
        select(BookmarkSimilarityCluster).where(
            BookmarkSimilarityCluster.user_id == user_id,
            BookmarkSimilarityCluster.run_id == preview.run.id,
            BookmarkSimilarityCluster.id == cluster_id,
        )
    )
    if cluster is None:
        raise persistence.BookmarkPersistenceNotFoundError("相似书签组不存在")
    decision_version = await _decision_version(
        session,
        user_id=user_id,
        run_id=preview.run.id,
        require_state=True,
    )
    scope = queries._cursor_scope(
        user_id=user_id,
        job_id=job_id,
        run_id=preview.run.id,
        job_version=preview.job.version,
        preview_version=preview.job.preview_version,
        endpoint="similarity_members",
        filters={
            "cluster_id": cluster_id,
        },
    )
    conditions = [
        BookmarkSimilarityClusterMember.user_id == user_id,
        BookmarkSimilarityClusterMember.run_id == preview.run.id,
        BookmarkSimilarityClusterMember.cluster_id == cluster_id,
    ]
    if cursor is not None:
        sequence, item_id = queries._decode_cursor(cursor, scope=scope)
        conditions.append(
            (BookmarkSimilarityClusterMember.first_source_sequence > sequence)
            | (
                (BookmarkSimilarityClusterMember.first_source_sequence == sequence)
                & (BookmarkSimilarityClusterMember.candidate_id > item_id)
            )
        )
    rows = (
        await session.execute(
            select(
                BookmarkSimilarityClusterMember.candidate_id.label("candidate_id"),
                BookmarkSimilarityClusterMember.first_source_sequence.label(
                    "first_source_sequence"
                ),
                BookmarkSimilarityClusterMember.is_canonical.label("is_canonical"),
                BookmarkStagingCandidate.display_title.label("title"),
                BookmarkStagingCandidate.identity_url.label("identity_url"),
                BookmarkStagingCandidate.occurrence_count.label("occurrence_count"),
            )
            .join(
                BookmarkStagingCandidate,
                and_(
                    BookmarkStagingCandidate.user_id
                    == BookmarkSimilarityClusterMember.user_id,
                    BookmarkStagingCandidate.run_id
                    == BookmarkSimilarityClusterMember.run_id,
                    BookmarkStagingCandidate.id
                    == BookmarkSimilarityClusterMember.candidate_id,
                ),
            )
            .where(*conditions)
            .order_by(
                BookmarkSimilarityClusterMember.first_source_sequence,
                BookmarkSimilarityClusterMember.candidate_id,
            )
            .limit(limit + 1)
        )
    ).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = (
        queries._encode_cursor(
            sequence=rows[-1].first_source_sequence,
            item_id=rows[-1].candidate_id,
            scope=scope,
        )
        if has_more
        else None
    )
    return BookmarkSimilarityMemberPageResponse(
        items=[_member_response(row) for row in rows],
        next_cursor=next_cursor,
        decision_version=decision_version,
    )


async def _decision_response(
    session: AsyncSession,
    *,
    user_id: str,
    job_id: str,
    run_id: str,
    job_version: int,
    base_create_count: int,
) -> BookmarkSimilarityDecisionResponse:
    statistics = await similarity_statistics(
        session,
        user_id,
        run_id,
        base_create_count=base_create_count,
    )
    return BookmarkSimilarityDecisionResponse(
        job_id=job_id,
        run_id=run_id,
        job_version=job_version,
        decision_version=statistics.decision_version,
        similarity_decision_counts=statistics.decision_counts,
        selected_merge_reduction_count=statistics.selected_merge_reduction_count,
        projected_create_count=statistics.projected_create_count,
    )


async def _base_create_count(session: AsyncSession, user_id: str, run_id: str) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(BookmarkStagingCandidate)
            .where(
                BookmarkStagingCandidate.user_id == user_id,
                BookmarkStagingCandidate.run_id == run_id,
                BookmarkStagingCandidate.proposed_action == "create",
            )
        )
        or 0
    )


async def set_similarity_decision(
    session: AsyncSession,
    user_id: str,
    job_id: str,
    cluster_id: str,
    *,
    expected_job_version: int,
    expected_decision_version: int,
    decision: SimilarityDecision,
) -> BookmarkSimilarityDecisionResponse:
    preview = await queries._current_complete_preview(session, user_id, job_id)
    if (
        preview.job.version != expected_job_version
        or preview.job.state not in {"parse_preview_ready", "final_preview_ready"}
    ):
        raise persistence.BookmarkPersistenceConflictError(
            "导入预览已发生变化，请刷新后重试"
        )
    cluster_exists = await session.scalar(
        select(BookmarkSimilarityCluster.id).where(
            BookmarkSimilarityCluster.user_id == user_id,
            BookmarkSimilarityCluster.run_id == preview.run.id,
            BookmarkSimilarityCluster.id == cluster_id,
        )
    )
    if cluster_exists is None:
        raise persistence.BookmarkPersistenceNotFoundError("相似书签组不存在")
    job_lock = await session.execute(
        update(BookmarkImportJob)
        .where(
            BookmarkImportJob.user_id == user_id,
            BookmarkImportJob.id == job_id,
            BookmarkImportJob.version == expected_job_version,
            BookmarkImportJob.state.in_(("parse_preview_ready", "final_preview_ready")),
        )
        .values(updated_at=BookmarkImportJob.updated_at)
    )
    if job_lock.rowcount != 1:
        await session.rollback()
        raise persistence.BookmarkPersistenceConflictError(
            "导入预览已发生变化，请刷新后重试"
        )
    current = await session.scalar(
        select(BookmarkSimilarityDecision.decision).where(
            BookmarkSimilarityDecision.user_id == user_id,
            BookmarkSimilarityDecision.run_id == preview.run.id,
            BookmarkSimilarityDecision.cluster_id == cluster_id,
        )
    )
    version = await _decision_version(
        session,
        user_id=user_id,
        run_id=preview.run.id,
        require_state=True,
    )
    if version != expected_decision_version:
        await session.rollback()
        raise persistence.BookmarkPersistenceConflictError(
            "相似书签选择已发生变化，请刷新后重试"
        )
    base_create_count = await _base_create_count(session, user_id, preview.run.id)
    if current == decision:
        response = await _decision_response(
            session,
            user_id=user_id,
            job_id=job_id,
            run_id=preview.run.id,
            job_version=preview.job.version,
            base_create_count=base_create_count,
        )
        await session.commit()
        return response

    advanced = await session.execute(
        update(BookmarkSimilarityDecisionState)
        .where(
            BookmarkSimilarityDecisionState.user_id == user_id,
            BookmarkSimilarityDecisionState.run_id == preview.run.id,
            BookmarkSimilarityDecisionState.version == expected_decision_version,
        )
        .values(
            version=BookmarkSimilarityDecisionState.version + 1,
            updated_at=utc_now(),
        )
    )
    if advanced.rowcount != 1:
        await session.rollback()
        raise persistence.BookmarkPersistenceConflictError(
            "相似书签选择已发生变化，请刷新后重试"
        )
    statement = sqlite_insert(BookmarkSimilarityDecision).values(
        user_id=user_id,
        run_id=preview.run.id,
        cluster_id=cluster_id,
        decision=decision,
        updated_at=utc_now(),
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=["user_id", "run_id", "cluster_id"],
            set_={"decision": decision, "updated_at": utc_now()},
        )
    )
    await session.commit()
    return await _decision_response(
        session,
        user_id=user_id,
        job_id=job_id,
        run_id=preview.run.id,
        job_version=preview.job.version,
        base_create_count=base_create_count,
    )


async def resolve_all_similarity_clusters(
    session: AsyncSession,
    user_id: str,
    job_id: str,
    *,
    expected_job_version: int,
    expected_decision_version: int,
) -> BookmarkSimilarityDecisionResponse:
    preview = await queries._current_complete_preview(session, user_id, job_id)
    if (
        preview.job.version != expected_job_version
        or preview.job.state not in {"parse_preview_ready", "final_preview_ready"}
    ):
        raise persistence.BookmarkPersistenceConflictError(
            "导入预览已发生变化，请刷新后重试"
        )
    job_lock = await session.execute(
        update(BookmarkImportJob)
        .where(
            BookmarkImportJob.user_id == user_id,
            BookmarkImportJob.id == job_id,
            BookmarkImportJob.version == expected_job_version,
            BookmarkImportJob.state.in_(("parse_preview_ready", "final_preview_ready")),
        )
        .values(updated_at=BookmarkImportJob.updated_at)
    )
    if job_lock.rowcount != 1:
        await session.rollback()
        raise persistence.BookmarkPersistenceConflictError(
            "导入预览已发生变化，请刷新后重试"
        )
    version = await _decision_version(
        session,
        user_id=user_id,
        run_id=preview.run.id,
        require_state=True,
    )
    if version != expected_decision_version:
        await session.rollback()
        raise persistence.BookmarkPersistenceConflictError(
            "相似书签选择已发生变化，请刷新后重试"
        )
    base_create_count = await _base_create_count(session, user_id, preview.run.id)
    unresolved = int(
        await session.scalar(
            select(func.count())
            .select_from(BookmarkSimilarityCluster)
            .outerjoin(BookmarkSimilarityDecision, _decision_join())
            .where(
                BookmarkSimilarityCluster.user_id == user_id,
                BookmarkSimilarityCluster.run_id == preview.run.id,
                BookmarkSimilarityDecision.cluster_id.is_(None),
            )
        )
        or 0
    )
    if unresolved == 0:
        response = await _decision_response(
            session,
            user_id=user_id,
            job_id=job_id,
            run_id=preview.run.id,
            job_version=preview.job.version,
            base_create_count=base_create_count,
        )
        await session.commit()
        return response

    advanced = await session.execute(
        update(BookmarkSimilarityDecisionState)
        .where(
            BookmarkSimilarityDecisionState.user_id == user_id,
            BookmarkSimilarityDecisionState.run_id == preview.run.id,
            BookmarkSimilarityDecisionState.version == expected_decision_version,
        )
        .values(
            version=BookmarkSimilarityDecisionState.version + 1,
            updated_at=utc_now(),
        )
    )
    if advanced.rowcount != 1:
        await session.rollback()
        raise persistence.BookmarkPersistenceConflictError(
            "相似书签选择已发生变化，请刷新后重试"
        )
    unresolved_clusters = (
        select(
            BookmarkSimilarityCluster.user_id,
            BookmarkSimilarityCluster.run_id,
            BookmarkSimilarityCluster.id,
            literal("keep_originals"),
            literal(utc_now()),
        )
        .outerjoin(BookmarkSimilarityDecision, _decision_join())
        .where(
            BookmarkSimilarityCluster.user_id == user_id,
            BookmarkSimilarityCluster.run_id == preview.run.id,
            BookmarkSimilarityDecision.cluster_id.is_(None),
        )
    )
    await session.execute(
        sqlite_insert(BookmarkSimilarityDecision).from_select(
            ["user_id", "run_id", "cluster_id", "decision", "updated_at"],
            unresolved_clusters,
        )
    )
    await session.commit()
    return await _decision_response(
        session,
        user_id=user_id,
        job_id=job_id,
        run_id=preview.run.id,
        job_version=preview.job.version,
        base_create_count=base_create_count,
    )


__all__ = [
    "SimilarityStatistics",
    "list_similarity_clusters",
    "list_similarity_members",
    "resolve_all_similarity_clusters",
    "set_similarity_decision",
    "similarity_statistics",
]
