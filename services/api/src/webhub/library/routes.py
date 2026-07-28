from __future__ import annotations

from collections.abc import Awaitable
from typing import Annotated, Literal

import pydantic
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from webhub.agent.provider_binding import resolve_optional_binding
from webhub.auth.dependencies import (
    CurrentIdentityDependency,
    DatabaseSessionDependency,
    require_trusted_origin,
)
from webhub.ingestion import backfill as ingestion_backfill
from webhub.ingestion import service as ingestion_service
from webhub.ingestion import worker as ingestion_worker
from webhub.ingestion.enrichment import AnalysisIntent
from webhub.library import batch, service
from webhub.library.schemas import (
    CategoryCreateRequest,
    CategoryDeletePreviewResponse,
    CategoryDeleteResponse,
    CategoryListResponse,
    CategoryResponse,
    CategoryUpdateRequest,
    MetadataBackfillProgressResponse,
    MetadataBackfillStartResponse,
    SiteAnalysisBackfillResponse,
    SiteBatchItemResponse,
    SiteBatchRequest,
    SiteBatchResponse,
    SiteBulkDeleteRequest,
    SiteBulkDeleteResponse,
    SiteCreateRequest,
    SiteDeleteResponse,
    SiteListResponse,
    SiteReorderRequest,
    SiteResponse,
    SiteSelectionResponse,
    SiteUpdateRequest,
    TagCreateRequest,
    TagDeleteResponse,
    TagListResponse,
    TagResponse,
    TagUpdateRequest,
)
from webhub.search.vectors import has_embeddings

router = APIRouter(prefix="/library", tags=["library"])
_VISIBLE_ANALYSIS_LIMIT = 8
_MAX_ANALYSIS_BACKFILL_REQUEST = 5_000


def _schedule_analysis(request: Request, *, user_id: str, site_id: str) -> None:
    scheduled = ingestion_worker.schedule_analysis(
        request.app.state.database,
        user_id=str(user_id),
        site_ids=(site_id,),
        priority=True,
        intent=AnalysisIntent.SITE_ENRICHMENT,
    )
    # When the foreground queue is full, leave the newly created/retargeted
    # site to the database-driven sweep instead of silently giving up until a
    # later list request happens to wake it.
    if scheduled.rejected:
        ingestion_worker.ensure_auto_backfill(
            request.app.state.database,
            user_id=str(user_id),
            rescan_if_running=True,
        )


WriteOriginDependency = Annotated[None, Depends(require_trusted_origin)]


async def _call[T](operation: Awaitable[T]) -> T:
    try:
        return await operation
    except service.LibraryError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code, "message": error.message},
        ) from error


async def _require_model_provider(request: Request, *, user_id: str) -> None:
    """Fail before a user-triggered LLM workflow creates or joins work."""

    async with request.app.state.database.sessions() as provider_session:
        binding = await resolve_optional_binding(
            provider_session,
            request.app.state.settings,
            user_id=user_id,
            kind="model",
        )
        await provider_session.rollback()
    if binding is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "model_provider_required",
                "message": "请先配置并启用模型 Provider，再开始 LLM 网站资料分析。",
            },
        )


def _metadata_backfill_response(
    progress: ingestion_backfill.MetadataBackfillProgress,
) -> MetadataBackfillProgressResponse:
    return MetadataBackfillProgressResponse(
        id=progress.id,
        status=progress.status,  # type: ignore[arg-type]
        total_count=progress.total_count,
        queued_count=progress.queued_count,
        running_count=progress.running_count,
        completed_count=progress.completed_count,
        complete_count=progress.complete_count,
        limited_count=progress.limited_count,
        failed_count=progress.failed_count,
        skipped_count=progress.skipped_count,
    )


def _metadata_backfill_start_response(
    started: ingestion_backfill.MetadataBackfillStart,
) -> MetadataBackfillStartResponse:
    progress = _metadata_backfill_response(started.progress)
    return MetadataBackfillStartResponse(
        **progress.model_dump(),
        reused=started.reused,
    )


@router.get("/categories", response_model=CategoryListResponse)
async def categories(
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
) -> CategoryListResponse:
    return await _call(service.list_categories(session, identity.user.id))


@router.post(
    "/categories",
    response_model=CategoryResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_category(
    payload: CategoryCreateRequest,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
) -> CategoryResponse:
    return await _call(
        service.create_category(session, identity.user.id, payload.name, payload.icon)
    )


@router.patch("/categories/{category_id}", response_model=CategoryResponse)
async def rename_category(
    category_id: str,
    payload: CategoryUpdateRequest,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
) -> CategoryResponse:
    return await _call(
        service.update_category(session, identity.user.id, category_id, payload.name, payload.icon)
    )


@router.get(
    "/categories/{category_id}/delete-preview",
    response_model=CategoryDeletePreviewResponse,
)
async def preview_category_delete(
    category_id: str,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
) -> CategoryDeletePreviewResponse:
    return await _call(service.category_delete_preview(session, identity.user.id, category_id))


@router.delete("/categories/{category_id}", response_model=CategoryDeleteResponse)
async def remove_category(
    category_id: str,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
) -> CategoryDeleteResponse:
    return await _call(service.delete_category(session, identity.user.id, category_id))


@router.get("/tags", response_model=TagListResponse)
async def tags(
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
) -> TagListResponse:
    return await _call(service.list_tags(session, identity.user.id))


@router.post("/tags", response_model=TagResponse, status_code=status.HTTP_201_CREATED)
async def add_tag(
    payload: TagCreateRequest,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
) -> TagResponse:
    return await _call(service.create_tag(session, identity.user.id, payload.name))


@router.patch("/tags/{tag_id}", response_model=TagResponse)
async def rename_tag(
    tag_id: str,
    payload: TagUpdateRequest,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
) -> TagResponse:
    return await _call(service.update_tag(session, identity.user.id, tag_id, payload.name))


@router.delete("/tags/{tag_id}", response_model=TagDeleteResponse)
async def remove_tag(
    tag_id: str,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
) -> TagDeleteResponse:
    return await _call(service.delete_tag(session, identity.user.id, tag_id))


@router.get("/sites", response_model=SiteListResponse)
async def sites(
    request: Request,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    q: Annotated[str | None, Query(max_length=300)] = None,
    category_id: str | None = None,
    tag_id: str | None = None,
    space_id: str | None = None,
    pinned: bool | None = None,
    sort: Literal["created", "updated", "name", "custom", "relevance"] = "updated",
    direction: Literal["asc", "desc"] = "desc",
    cursor: Annotated[str | None, Query(max_length=2_048)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> SiteListResponse:
    # 解析 Provider 的代价不只是读库解密：resolve_binding 会重新做一次
    # DNS 解析（保存后主机名可能被指向内网，这道 SSRF 校验必须留着），
    # 默认超时以秒计。搜索是按键去抖的高频请求，不能每次都付这个代价。
    # 所以两道门都过了才解析：① 按相关度排序 ② 该账号真有向量。
    # 没有向量时语义召回一行都贡献不了，跳过是等价的，不是偷工减料。
    binding = None
    if sort == "relevance" and await has_embeddings(session, str(identity.user.id)):
        binding = await resolve_optional_binding(
            session,
            request.app.state.settings,
            user_id=str(identity.user.id),
            kind="embedding",
        )
    result = await _call(
        service.list_sites(
            session,
            identity.user.id,
            q=q,
            category_id=category_id,
            tag_id=tag_id,
            space_id=space_id,
            pinned=pinned,
            sort=sort,
            direction=direction,
            cursor=cursor,
            limit=limit,
            embedding_binding=binding,
        )
    )
    visible_site_ids = tuple(
        site.id for site in result.items if site.analysis_status == "not_analyzed"
    )[:_VISIBLE_ANALYSIS_LIMIT]
    if visible_site_ids:
        ingestion_worker.schedule_analysis(
            request.app.state.database,
            user_id=str(identity.user.id),
            site_ids=visible_site_ids,
            priority=True,
        )
    ingestion_worker.ensure_auto_backfill(
        request.app.state.database,
        user_id=str(identity.user.id),
    )
    return result


@router.post("/sites", response_model=SiteResponse, status_code=status.HTTP_201_CREATED)
async def add_site(
    payload: SiteCreateRequest,
    request: Request,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
) -> SiteResponse:
    created = await _call(service.create_site(session, identity.user.id, payload))
    # 脱钩执行：保存一个网站不该等在别人家的慢服务器上，更不该因为对方宕机而失败。
    _schedule_analysis(request, user_id=identity.user.id, site_id=created.id)
    return created


@router.get("/sites/selection", response_model=SiteSelectionResponse)
async def site_selection(
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    q: Annotated[str | None, Query(max_length=300)] = None,
    category_id: str | None = None,
    tag_id: str | None = None,
    pinned: bool | None = None,
) -> SiteSelectionResponse:
    return await _call(
        service.list_site_selection(
            session,
            identity.user.id,
            q=q,
            category_id=category_id,
            tag_id=tag_id,
            pinned=pinned,
        )
    )


@router.post("/sites/bulk-delete", response_model=SiteBulkDeleteResponse)
async def bulk_delete_sites(
    payload: SiteBulkDeleteRequest,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
) -> SiteBulkDeleteResponse:
    return await _call(service.bulk_delete_sites(session, identity.user.id, payload))


@router.get("/sites/{site_id}", response_model=SiteResponse)
async def site(
    site_id: str,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
) -> SiteResponse:
    return await _call(service.get_site(session, identity.user.id, site_id))


@router.patch("/sites/{site_id}", response_model=SiteResponse)
async def edit_site(
    site_id: str,
    payload: SiteUpdateRequest,
    request: Request,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
) -> SiteResponse:
    updated = await _call(service.update_site(session, identity.user.id, site_id, payload))
    if "url" in payload.model_fields_set and updated.analysis_status == "not_analyzed":
        _schedule_analysis(request, user_id=identity.user.id, site_id=updated.id)
    return updated


@router.delete("/sites/{site_id}", response_model=SiteDeleteResponse)
async def remove_site(
    site_id: str,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
    expected_version: Annotated[int, Query(ge=1)],
) -> SiteDeleteResponse:
    return await _call(
        service.delete_site(
            session,
            identity.user.id,
            site_id,
            expected_version=expected_version,
        )
    )


@router.post("/sites/{site_id}/analyze", response_model=SiteResponse)
async def analyze_site(
    site_id: str,
    request: Request,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
) -> SiteResponse:
    """Fetch public page evidence and run the three constrained LLM tools.

    The model draft is not a write capability. The ingestion service validates
    it and atomically stores only fields that are not protected as user input.
    This endpoint waits because it is driven by an explicit detail-page action.
    """

    await _require_model_provider(request, user_id=str(identity.user.id))
    try:
        await ingestion_worker.analyze_and_wait(
            request.app.state.database,
            user_id=str(identity.user.id),
            site_id=site_id,
        )
    except ingestion_worker.AnalysisQueueFullError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "analysis_queue_full", "message": str(error)},
        ) from error
    return await _call(service.get_site(session, identity.user.id, site_id))


@router.post("/sites/batch", response_model=SiteBatchResponse)
async def batch_sites(
    payload: SiteBatchRequest,
    request: Request,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
) -> SiteBatchResponse:
    """Preview or commit a batch of URLs.

    ``confirm=false`` is read-only: it classifies every URL and touches nothing,
    which is what makes "确认前主数据无变化" a property of the endpoint rather
    than a promise.  ``confirm=true`` creates the importable ones, each item
    independently, so one failure cannot take the rest down with it.
    """

    urls = payload.urls if payload.urls is not None else batch.extract_urls(payload.text or "")
    if not urls:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "no_urls", "message": "没有解析到任何 http(s) 网址"},
        )

    items = (
        await batch.create_batch(session, identity.user.id, urls)
        if payload.confirm
        else await batch.preview_batch(session, identity.user.id, urls)
    )
    names = ("ready", "duplicate", "invalid", "created", "failed")
    counts = dict.fromkeys(names, 0)
    for item in items:
        counts[item.status] = counts.get(item.status, 0) + 1
    scheduled = ingestion_worker.schedule_analysis(
        request.app.state.database,
        user_id=str(identity.user.id),
        site_ids=tuple(
            item.site_id for item in items if item.status == "created" and item.site_id is not None
        ),
        intent=AnalysisIntent.SITE_ENRICHMENT,
    )
    if scheduled.rejected:
        ingestion_worker.ensure_auto_backfill(
            request.app.state.database,
            user_id=str(identity.user.id),
            rescan_if_running=True,
        )
    return SiteBatchResponse(
        confirmed=payload.confirm,
        total=len(items),
        ready=counts["ready"],
        duplicate=counts["duplicate"],
        invalid=counts["invalid"],
        created=counts["created"],
        failed=counts["failed"],
        items=[
            SiteBatchItemResponse(
                url=item.url,
                status=item.status,
                reason=item.reason,
                site_id=item.site_id,
            )
            for item in items
        ],
    )


@router.post(
    "/sites/analyze-missing",
    response_model=SiteAnalysisBackfillResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def analyze_missing_sites(
    request: Request,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
    limit: Annotated[
        int,
        Query(ge=1, le=_MAX_ANALYSIS_BACKFILL_REQUEST),
    ] = _MAX_ANALYSIS_BACKFILL_REQUEST,
) -> SiteAnalysisBackfillResponse:
    """Queue one bounded, user-triggered batch of historical unanalysed sites."""

    user_id = str(identity.user.id)
    bounded_limit = min(limit, ingestion_worker.MAX_QUEUED_ANALYSES_PER_ACCOUNT)
    site_ids, remaining = await ingestion_service.not_analyzed_site_ids(
        session,
        user_id,
        limit=bounded_limit,
        excluded_site_ids=ingestion_worker.pending_site_ids(request.app.state.database, user_id),
    )
    scheduled = ingestion_worker.schedule_analysis(
        request.app.state.database,
        user_id=user_id,
        site_ids=tuple(site_ids),
        priority=False,
    )
    if scheduled.rejected:
        ingestion_worker.ensure_auto_backfill(
            request.app.state.database,
            user_id=user_id,
            rescan_if_running=True,
        )
    return SiteAnalysisBackfillResponse(
        queued_count=scheduled.queued,
        active_count=len(
            ingestion_worker.pending_site_ids(request.app.state.database, user_id)
        ),
        remaining_count=remaining + scheduled.rejected,
    )


@router.post(
    "/metadata-backfills",
    response_model=MetadataBackfillStartResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_metadata_backfill(
    request: Request,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
) -> MetadataBackfillStartResponse:
    """Start (or join) the account's durable LLM website-enrichment run."""

    user_id = str(identity.user.id)
    await _require_model_provider(request, user_id=user_id)
    started = await ingestion_backfill.start_metadata_backfill(session, user_id=user_id)
    if started.progress.is_active:
        ingestion_worker.ensure_metadata_backfill(
            request.app.state.database,
            user_id=user_id,
            run_id=started.progress.id,
        )
    return _metadata_backfill_start_response(started)


@router.get(
    "/metadata-backfills/active",
    response_model=MetadataBackfillProgressResponse,
    responses={status.HTTP_204_NO_CONTENT: {"description": "No active metadata backfill"}},
)
async def active_metadata_backfill(
    request: Request,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
) -> MetadataBackfillProgressResponse | Response:
    """Let a newly loaded page reattach to the account's durable task."""

    user_id = str(identity.user.id)
    progress = await ingestion_backfill.active_metadata_backfill(session, user_id=user_id)
    if progress is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    ingestion_worker.ensure_metadata_backfill(
        request.app.state.database,
        user_id=user_id,
        run_id=progress.id,
    )
    return _metadata_backfill_response(progress)


@router.get(
    "/metadata-backfills/{run_id}",
    response_model=MetadataBackfillProgressResponse,
)
async def metadata_backfill_progress(
    run_id: str,
    request: Request,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
) -> MetadataBackfillProgressResponse:
    """Read one fixed task snapshot and opportunistically wake its worker."""

    user_id = str(identity.user.id)
    progress = await ingestion_backfill.progress_for_run(
        session,
        user_id=user_id,
        run_id=run_id,
    )
    if progress is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "metadata_backfill_not_found", "message": "补全任务不存在"},
        )
    if progress.is_active:
        ingestion_worker.ensure_metadata_backfill(
            request.app.state.database,
            user_id=user_id,
            run_id=progress.id,
        )
    return _metadata_backfill_response(progress)


@router.post("/categories/{category_id}/reorder", status_code=status.HTTP_204_NO_CONTENT)
async def reorder_category_sites(
    category_id: str,
    payload: SiteReorderRequest,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
) -> None:
    """Move sites within one category.

    Takes a list rather than a single id so a multi-select drag is one request:
    the backend has always been able to move a block, the earlier Space UI just
    never sent more than one.
    """

    await _call(
        service.reorder_sites(
            session,
            identity.user.id,
            category_id,
            ordered_site_ids=list(payload.ordered_site_ids),
            before_site_id=payload.before_site_id,
        )
    )


class ReclassifyApplyRequest(pydantic.BaseModel):
    expected_categories: dict[str, str]
    expected_versions: dict[str, int]


@router.post("/reclassify/propose")
async def propose_library_reclassification(
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
) -> dict[str, object]:
    """Build a reclassification draft with token/cost estimations. Zero model calls."""

    from webhub.library import reclassify

    try:
        return await reclassify.propose_reclassification(session, identity.user.id)
    except Exception as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="暂时无法生成重分类草稿，请稍后重试。",
        ) from error


@router.post("/reclassify/apply")
async def apply_library_reclassification(
    payload: ReclassifyApplyRequest,
    request: Request,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
) -> dict[str, object]:
    """Execute LLM reclassification on the account's sites."""

    from webhub.library import reclassify

    try:
        return await reclassify.apply_reclassification(
            session,
            identity.user.id,
            expected_categories=payload.expected_categories,
            expected_versions=payload.expected_versions,
            cancel_requested=request.is_disconnected,
        )
    except reclassify.ReclassificationError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail=error.safe_message,
        ) from error
    except Exception as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="重分类暂时无法完成，结果未写入，请稍后重试。",
        ) from error
