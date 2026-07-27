from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Annotated, Literal

import pydantic
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from webhub.agent.provider_binding import resolve_optional_binding
from webhub.auth.dependencies import (
    CurrentIdentityDependency,
    DatabaseSessionDependency,
    require_trusted_origin,
)
from webhub.ingestion import service as ingestion_service
from webhub.library import batch, service
from webhub.library.schemas import (
    CategoryCreateRequest,
    CategoryDeletePreviewResponse,
    CategoryDeleteResponse,
    CategoryListResponse,
    CategoryResponse,
    CategoryUpdateRequest,
    SiteBatchItemResponse,
    SiteBatchRequest,
    SiteBatchResponse,
    SiteCreateRequest,
    SiteDeleteResponse,
    SiteListResponse,
    SiteReorderRequest,
    SiteResponse,
    SiteUpdateRequest,
    TagCreateRequest,
    TagDeleteResponse,
    TagListResponse,
    TagResponse,
    TagUpdateRequest,
)
from webhub.search.vectors import has_embeddings

router = APIRouter(prefix="/library", tags=["library"])

# 强引用住脱钩的分析任务：asyncio 只持弱引用，不这么做任务可能在抓取中途被 GC。
_ANALYSIS_TASKS: set[asyncio.Task[None]] = set()


def _schedule_analysis(request: Request, *, user_id: str, site_id: str) -> None:
    task = asyncio.create_task(
        ingestion_service.analyze_in_background(
            request.app.state.database,
            user_id,
            site_id,
        )
    )
    _ANALYSIS_TASKS.add(task)
    task.add_done_callback(_ANALYSIS_TASKS.discard)
WriteOriginDependency = Annotated[None, Depends(require_trusted_origin)]


async def _call[T](operation: Awaitable[T]) -> T:
    try:
        return await operation
    except service.LibraryError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code, "message": error.message},
        ) from error


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
    return await _call(service.create_category(session, identity.user.id, payload.name))


@router.patch("/categories/{category_id}", response_model=CategoryResponse)
async def rename_category(
    category_id: str,
    payload: CategoryUpdateRequest,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
) -> CategoryResponse:
    return await _call(
        service.update_category(session, identity.user.id, category_id, payload.name)
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
    return await _call(
        service.category_delete_preview(session, identity.user.id, category_id)
    )


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
    return await _call(
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
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
) -> SiteResponse:
    return await _call(service.update_site(session, identity.user.id, site_id, payload))


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
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
) -> SiteResponse:
    """Re-read the page's public metadata and store what it fills in.

    Synchronous on purpose, unlike the analysis scheduled at creation time:
    this one is a button the user just pressed, so they should get the result
    rather than a spinner that never resolves on its own.
    """

    outcome = await ingestion_service.analyze_site(session, identity.user.id, site_id)
    if outcome is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "网站不存在"},
        )
    return await _call(service.get_site(session, identity.user.id, site_id))


@router.post("/sites/batch", response_model=SiteBatchResponse)
async def batch_sites(
    payload: SiteBatchRequest,
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
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(error),
        ) from error


@router.post("/reclassify/apply")
async def apply_library_reclassification(
    payload: ReclassifyApplyRequest,
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
            expected_versions=payload.expected_versions,
        )
    except reclassify.ReclassificationError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error.safe_message,
        ) from error
    except Exception as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(error),
        ) from error


