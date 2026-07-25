from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.auth.passwords import password_manager
from webhub.auth.security import new_session_token, normalize_username, token_hash
from webhub.db.models import LoginSession, User, UserPreference


class UsernameTakenError(ValueError):
    pass


class InvalidCredentialsError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class IssuedSession:
    user: User
    preferences: UserPreference
    login_session: LoginSession
    raw_token: str


async def register_user(
    session: AsyncSession,
    *,
    username: str,
    password: str,
    display_name: str | None,
    ttl_seconds: int,
) -> IssuedSession:
    normalized_username = normalize_username(username)
    selected_display_name = (display_name or username).strip()
    if not selected_display_name:
        raise ValueError("显示名称不能为空")
    existing = await session.scalar(select(User.id).where(User.username == normalized_username))
    if existing:
        raise UsernameTakenError

    user = User(
        username=normalized_username,
        display_name=selected_display_name,
        password_hash=password_manager.hash(password),
    )
    preferences = UserPreference(user=user)
    raw_token, login_session = _new_login_session(user, ttl_seconds)
    session.add_all((user, preferences, login_session))
    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise UsernameTakenError from error
    return IssuedSession(user, preferences, login_session, raw_token)


async def authenticate_user(
    session: AsyncSession,
    *,
    username: str,
    password: str,
    ttl_seconds: int,
) -> IssuedSession:
    try:
        normalized_username = normalize_username(username)
    except ValueError:
        password_manager.verify_dummy(password)
        raise InvalidCredentialsError from None

    row = (
        await session.execute(
            select(User, UserPreference)
            .join(UserPreference, UserPreference.user_id == User.id)
            .where(User.username == normalized_username, User.is_active.is_(True))
        )
    ).one_or_none()
    if row is None:
        password_manager.verify_dummy(password)
        raise InvalidCredentialsError

    user, preferences = row
    if not password_manager.verify(user.password_hash, password):
        raise InvalidCredentialsError
    if password_manager.needs_rehash(user.password_hash):
        user.password_hash = password_manager.hash(password)

    raw_token, login_session = _new_login_session(user, ttl_seconds)
    session.add(login_session)
    await session.commit()
    return IssuedSession(user, preferences, login_session, raw_token)


def _new_login_session(user: User, ttl_seconds: int) -> tuple[str, LoginSession]:
    raw_token = new_session_token()
    login_session = LoginSession(
        user=user,
        token_hash=token_hash(raw_token),
        expires_at=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
    )
    return raw_token, login_session


async def change_password(
    session: AsyncSession,
    *,
    user: User,
    current_session_id: str,
    current_password: str,
    new_password: str,
) -> None:
    if not password_manager.verify(user.password_hash, current_password):
        raise InvalidCredentialsError
    user.password_hash = password_manager.hash(new_password)
    await session.execute(
        update(LoginSession)
        .where(
            LoginSession.user_id == user.id,
            LoginSession.id != current_session_id,
            LoginSession.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(UTC))
    )
    await session.commit()
