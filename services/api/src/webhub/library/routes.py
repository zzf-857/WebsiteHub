from __future__ import annotations

from collections.abc import Awaitable
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status

from webhub.auth.dependencies import (
    CurrentIdentityDependency,
    DatabaseSessionDependency,
    require_trusted_origin,
)
from webhub.library import service
from webhub.library.schemas import (
    CategoryCreateRequest,
    CategoryDeletePreviewResponse,
    CategoryDeleteResponse,
    CategoryListResponse,
    CategoryResponse,
    CategoryUpdateRequest,
    SiteCreateRequest,
    SiteDeleteResponse,
    SiteListResponse,
    SiteResponse,
    SiteUpdateRequest,
    TagCreateRequest,
    TagDeleteResponse,
    TagListResponse,
    TagResponse,
    TagUpdateRequest,
)

router = APIRouter(prefix="/library", tags=["library"])
WriteOriginDependency = Annotated[None, Depends(require_trusted_origin)]


async def _call[T](operation: Awaitable[T]) -> T:
    try:
        return await operation
    except service.LibraryError as error:
        raise HTTPException(error.status_code, error.message) from error


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
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    q: Annotated[str | None, Query(max_length=300)] = None,
    category_id: str | None = None,
    tag_id: str | None = None,
    space_id: str | None = None,
    pinned: bool | None = None,
    sort: Literal["created", "updated", "name"] = "updated",
    direction: Literal["asc", "desc"] = "desc",
    cursor: Annotated[str | None, Query(max_length=2_048)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> SiteListResponse:
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
        )
    )


@router.post("/sites", response_model=SiteResponse, status_code=status.HTTP_201_CREATED)
async def add_site(
    payload: SiteCreateRequest,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
) -> SiteResponse:
    return await _call(service.create_site(session, identity.user.id, payload))


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
) -> SiteDeleteResponse:
    return await _call(service.delete_site(session, identity.user.id, site_id))
