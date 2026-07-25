from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from webhub.auth.dependencies import (
    CurrentIdentity,
    CurrentIdentityDependency,
    DatabaseSessionDependency,
    SettingsDependency,
    require_trusted_origin,
)
from webhub.auth.rate_limit import LoginRateLimiter, RateLimitExceededError
from webhub.auth.schemas import (
    AuthResponse,
    ChangePasswordRequest,
    LoginRequest,
    MessageResponse,
    PreferenceResponse,
    PreferenceUpdateRequest,
    RegisterRequest,
    UserResponse,
)
from webhub.auth.security import normalize_username, rate_limit_client_host, rate_limit_key
from webhub.auth.service import (
    InvalidCredentialsError,
    IssuedSession,
    UsernameTakenError,
    authenticate_user,
    change_password,
    register_user,
)
from webhub.config import Settings

router = APIRouter(
    prefix="/auth",
    tags=["auth"],
    dependencies=[Depends(require_trusted_origin)],
)


def _user_response(identity: CurrentIdentity | IssuedSession) -> UserResponse:
    preferences = identity.preferences
    return UserResponse(
        id=identity.user.id,
        username=identity.user.username,
        display_name=identity.user.display_name,
        created_at=identity.user.created_at,
        preferences=PreferenceResponse(theme=preferences.theme, locale=preferences.locale),
    )


def _set_session_cookie(
    response: Response,
    *,
    token: str,
    settings: Settings,
) -> None:
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )


def _rate_limiter(request: Request) -> LoginRateLimiter:
    return request.app.state.login_rate_limiter


def _rate_limit_keys(request: Request, username: str) -> tuple[str, str]:
    client_host = rate_limit_client_host(request)
    try:
        normalized_username = normalize_username(username)
    except ValueError:
        normalized_username = "invalid"
    return (
        rate_limit_key("client", client_host),
        rate_limit_key("account", client_host, normalized_username),
    )


@router.post("/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterRequest,
    response: Response,
    settings: SettingsDependency,
    session: DatabaseSessionDependency,
) -> AuthResponse:
    try:
        issued = await register_user(
            session,
            username=payload.username,
            password=payload.password,
            display_name=payload.display_name,
            ttl_seconds=settings.session_ttl_seconds,
        )
    except UsernameTakenError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, "用户名已被使用") from error
    except ValueError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
    _set_session_cookie(response, token=issued.raw_token, settings=settings)
    return AuthResponse(user=_user_response(issued))


@router.post("/login", response_model=AuthResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    session: DatabaseSessionDependency,
) -> AuthResponse:
    limiter = _rate_limiter(request)
    client_key, account_key = _rate_limit_keys(request, payload.username)
    try:
        attempt = limiter.reserve(client_key=client_key, account_key=account_key)
    except RateLimitExceededError as error:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "登录尝试过多，请稍后再试",
            headers={"Retry-After": str(error.retry_after)},
        ) from error
    try:
        issued = await authenticate_user(
            session,
            username=payload.username,
            password=payload.password,
            ttl_seconds=settings.session_ttl_seconds,
        )
    except InvalidCredentialsError as error:
        limiter.record_failure(attempt)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误") from error
    except BaseException:
        limiter.cancel(attempt)
        raise
    limiter.record_success(attempt)
    _set_session_cookie(response, token=issued.raw_token, settings=settings)
    return AuthResponse(user=_user_response(issued))


@router.get("/me", response_model=AuthResponse)
async def me(identity: CurrentIdentityDependency) -> AuthResponse:
    return AuthResponse(user=_user_response(identity))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response,
    identity: CurrentIdentityDependency,
    settings: SettingsDependency,
    session: DatabaseSessionDependency,
) -> None:
    identity.login_session.revoked_at = datetime.now(UTC)
    await session.commit()
    response.delete_cookie(
        settings.session_cookie_name,
        path="/",
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )


@router.patch("/preferences", response_model=AuthResponse)
async def update_preferences(
    payload: PreferenceUpdateRequest,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
) -> AuthResponse:
    identity.preferences.theme = payload.theme
    await session.commit()
    return AuthResponse(user=_user_response(identity))


@router.post("/change-password", response_model=MessageResponse)
async def update_password(
    payload: ChangePasswordRequest,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
) -> MessageResponse:
    try:
        await change_password(
            session,
            user=identity.user,
            current_session_id=identity.login_session.id,
            current_password=payload.current_password,
            new_password=payload.new_password,
        )
    except InvalidCredentialsError as error:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "当前密码错误") from error
    return MessageResponse(message="密码已更新，其他设备的登录已退出")
