from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.auth.security import token_hash, validate_request_origin
from webhub.config import Settings
from webhub.db.database import Database
from webhub.db.models import LoginSession, User, UserPreference


@dataclass(frozen=True, slots=True)
class CurrentIdentity:
    user: User
    preferences: UserPreference
    login_session: LoginSession


def request_settings(request: Request) -> Settings:
    return request.app.state.settings


SettingsDependency = Annotated[Settings, Depends(request_settings)]


async def database_session(request: Request) -> AsyncIterator[AsyncSession]:
    database: Database = request.app.state.database
    async with database.sessions() as session:
        yield session


DatabaseSessionDependency = Annotated[AsyncSession, Depends(database_session)]


def require_trusted_origin(
    request: Request,
    settings: SettingsDependency,
) -> None:
    validate_request_origin(request, settings)


async def require_current_identity(
    request: Request,
    settings: SettingsDependency,
    session: DatabaseSessionDependency,
) -> CurrentIdentity:
    raw_token = request.cookies.get(settings.session_cookie_name)
    if not raw_token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "请先登录")

    row = (
        await session.execute(
            select(User, UserPreference, LoginSession)
            .join(UserPreference, UserPreference.user_id == User.id)
            .join(LoginSession, LoginSession.user_id == User.id)
            .where(
                LoginSession.token_hash == token_hash(raw_token),
                LoginSession.revoked_at.is_(None),
                LoginSession.expires_at > datetime.now(UTC),
                User.is_active.is_(True),
            )
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录已失效")
    user, preferences, login_session = row
    return CurrentIdentity(user, preferences, login_session)


CurrentIdentityDependency = Annotated[CurrentIdentity, Depends(require_current_identity)]
