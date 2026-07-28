from __future__ import annotations

from collections.abc import Awaitable
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status

from webhub.auth.dependencies import (
    CurrentIdentityDependency,
    DatabaseSessionDependency,
    require_trusted_origin,
)
from webhub.spaces import service
from webhub.spaces.schemas import (
    SpaceCreateRequest,
    SpaceDeletePreviewResponse,
    SpaceDeleteResponse,
    SpaceDetailResponse,
    SpaceListResponse,
    SpaceMemberAddRequest,
    SpaceMemberAddResponse,
    SpaceMemberBatchRequest,
    SpaceMemberBatchResponse,
    SpaceMemberDeleteResponse,
    SpaceReorderRequest,
    SpaceResponse,
    SpaceUpdateRequest,
)

router = APIRouter(prefix="/spaces", tags=["spaces"])
WriteOriginDependency = Annotated[None, Depends(require_trusted_origin)]


async def _call[T](operation: Awaitable[T]) -> T:
    try:
        return await operation
    except service.SpaceError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code, "message": error.message},
        ) from error


@router.get("", response_model=SpaceListResponse)
async def spaces(
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    sort: Literal["created", "updated", "name"] = "updated",
    direction: Literal["asc", "desc"] = "desc",
    cursor: Annotated[str | None, Query(max_length=2_048)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> SpaceListResponse:
    return await _call(
        service.list_spaces(
            session,
            identity.user.id,
            sort=sort,
            direction=direction,
            cursor=cursor,
            limit=limit,
        )
    )


@router.post("", response_model=SpaceResponse, status_code=status.HTTP_201_CREATED)
async def add_space(
    payload: SpaceCreateRequest,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
) -> SpaceResponse:
    return await _call(service.create_space(session, identity.user.id, payload))


@router.post("/member-batches", response_model=SpaceMemberBatchResponse)
async def add_space_members_batch(
    payload: SpaceMemberBatchRequest,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
) -> SpaceMemberBatchResponse:
    return await _call(service.add_members_batch(session, identity.user.id, payload))


@router.get("/{space_id}", response_model=SpaceDetailResponse)
async def space(
    space_id: str,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    cursor: Annotated[str | None, Query(max_length=2_048)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> SpaceDetailResponse:
    return await _call(
        service.get_space(
            session,
            identity.user.id,
            space_id,
            cursor=cursor,
            limit=limit,
        )
    )


@router.patch("/{space_id}", response_model=SpaceResponse)
async def rename_space(
    space_id: str,
    payload: SpaceUpdateRequest,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
) -> SpaceResponse:
    return await _call(service.update_space(session, identity.user.id, space_id, payload))


@router.get(
    "/{space_id}/delete-preview",
    response_model=SpaceDeletePreviewResponse,
)
async def preview_space_delete(
    space_id: str,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
) -> SpaceDeletePreviewResponse:
    return await _call(service.delete_preview(session, identity.user.id, space_id))


@router.delete("/{space_id}", response_model=SpaceDeleteResponse)
async def remove_space(
    space_id: str,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
    expected_version: Annotated[int, Query(ge=1)],
) -> SpaceDeleteResponse:
    return await _call(
        service.delete_space(
            session,
            identity.user.id,
            space_id,
            expected_version=expected_version,
        )
    )


@router.post(
    "/{space_id}/members",
    response_model=SpaceMemberAddResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_space_member(
    space_id: str,
    payload: SpaceMemberAddRequest,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
) -> SpaceMemberAddResponse:
    return await _call(service.add_member(session, identity.user.id, space_id, payload))


@router.patch("/{space_id}/members/order", response_model=SpaceResponse)
async def reorder_space_members(
    space_id: str,
    payload: SpaceReorderRequest,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
) -> SpaceResponse:
    return await _call(service.reorder_members(session, identity.user.id, space_id, payload))


@router.delete(
    "/{space_id}/members/{site_id}",
    response_model=SpaceMemberDeleteResponse,
)
async def remove_space_member(
    space_id: str,
    site_id: str,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
    expected_version: Annotated[int, Query(ge=1)],
) -> SpaceMemberDeleteResponse:
    return await _call(
        service.remove_member(
            session,
            identity.user.id,
            space_id,
            site_id,
            expected_version=expected_version,
        )
    )
