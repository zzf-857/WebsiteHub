from __future__ import annotations

from collections.abc import Awaitable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from webhub.auth.dependencies import (
    CurrentIdentityDependency,
    DatabaseSessionDependency,
    SettingsDependency,
    require_trusted_origin,
)
from webhub.providers import service
from webhub.providers.rate_limit import (
    ProviderTestRateLimiter,
    ProviderTestRateLimitError,
)
from webhub.providers.registry import ProviderKind
from webhub.providers.schemas import (
    ExpectedVersionRequest,
    ProviderConnectionTestRequest,
    ProviderConnectionTestResponse,
    ProviderCreateRequest,
    ProviderDeleteResponse,
    ProviderListResponse,
    ProviderRegistryResponse,
    ProviderResponse,
    ProviderUpdateRequest,
)

router = APIRouter(prefix="/providers", tags=["providers"])
WriteOriginDependency = Annotated[None, Depends(require_trusted_origin)]


async def _call[T](operation: Awaitable[T]) -> T:
    try:
        return await operation
    except service.ProviderError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code, "message": error.message},
        ) from error


def _connection_test_limiter(
    request: Request,
    settings: SettingsDependency,
) -> ProviderTestRateLimiter:
    limiter = getattr(request.app.state, "provider_test_rate_limiter", None)
    if limiter is None:
        limiter = ProviderTestRateLimiter(
            attempts=settings.provider_test_rate_limit_attempts,
            window_seconds=settings.provider_test_rate_limit_window_seconds,
            max_accounts=settings.provider_test_max_tracked_accounts,
        )
        request.app.state.provider_test_rate_limiter = limiter
    return limiter


@router.get("/registry", response_model=ProviderRegistryResponse)
async def provider_registry(
    _: CurrentIdentityDependency,
) -> ProviderRegistryResponse:
    return service.registry()


@router.post(
    "/test-connection",
    response_model=ProviderConnectionTestResponse,
)
async def test_provider_connection(
    payload: ProviderConnectionTestRequest,
    request: Request,
    identity: CurrentIdentityDependency,
    settings: SettingsDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
) -> ProviderConnectionTestResponse:
    limiter = _connection_test_limiter(request, settings)
    try:
        limiter.record(identity.user.id)
    except ProviderTestRateLimitError as error:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "provider_test_rate_limited",
                "message": "连接测试请求过多，请稍后再试",
            },
            headers={"Retry-After": str(error.retry_after)},
        ) from error
    return await _call(
        service.test_connection(
            session,
            identity.user.id,
            settings,
            payload,
        )
    )


@router.get("", response_model=ProviderListResponse)
async def provider_configs(
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    kind: ProviderKind | None = None,
) -> ProviderListResponse:
    return await _call(service.list_configs(session, identity.user.id, kind=kind))


@router.post("", response_model=ProviderResponse, status_code=status.HTTP_201_CREATED)
async def add_provider_config(
    payload: ProviderCreateRequest,
    identity: CurrentIdentityDependency,
    settings: SettingsDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
) -> ProviderResponse:
    return await _call(
        service.create_config(
            session,
            identity.user.id,
            settings,
            payload,
        )
    )


@router.get("/{config_id}", response_model=ProviderResponse)
async def provider_config(
    config_id: str,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
) -> ProviderResponse:
    return await _call(service.get_config(session, identity.user.id, config_id))


@router.patch("/{config_id}", response_model=ProviderResponse)
async def edit_provider_config(
    config_id: str,
    payload: ProviderUpdateRequest,
    identity: CurrentIdentityDependency,
    settings: SettingsDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
) -> ProviderResponse:
    return await _call(
        service.update_config(
            session,
            identity.user.id,
            settings,
            config_id,
            payload,
        )
    )


@router.post("/{config_id}/enable", response_model=ProviderResponse)
async def activate_provider_config(
    config_id: str,
    payload: ExpectedVersionRequest,
    identity: CurrentIdentityDependency,
    settings: SettingsDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
) -> ProviderResponse:
    return await _call(
        service.enable_config(
            session,
            identity.user.id,
            settings,
            config_id,
            expected_version=payload.expected_version,
        )
    )


@router.delete("/{config_id}", response_model=ProviderDeleteResponse)
async def remove_provider_config(
    config_id: str,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    _: WriteOriginDependency,
    expected_version: Annotated[int, Query(ge=1)],
) -> ProviderDeleteResponse:
    return await _call(
        service.delete_config(
            session,
            identity.user.id,
            config_id,
            expected_version=expected_version,
        )
    )
