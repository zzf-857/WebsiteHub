"""Agent tokens and the Agent write audit log.

Rules (2026-10 D3):

* A token is ``whk_`` + ``secrets.token_urlsafe(32)``. Only its SHA-256 digest
  is stored; the plaintext is returned exactly once, by ``issue_token``.
* Scopes are ``read`` or ``read write`` — ``write`` always implies ``read``.
* Revoking keeps the row (``revoked_at``), so audit entries keep their token
  name. At most ``MAX_ACTIVE_TOKENS`` unrevoked tokens per account.
* ``last_used_at`` is written at most once per ``LAST_USED_RESOLUTION`` per
  token: a busy Agent must not turn every read into a database write.
"""

from __future__ import annotations

import secrets
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.auth.security import token_hash
from webhub.db.locking import reserve_account_taxonomy
from webhub.db.models import AgentAuditLog, AgentToken, User, utc_now

TOKEN_PREFIX = "whk_"
# ``token_urlsafe(32)`` is 43 characters; allow some slack but never hash
# arbitrarily long header values.
MAX_TOKEN_LENGTH = 128
DISPLAY_PREFIX_LENGTH = len(TOKEN_PREFIX) + 4
MAX_ACTIVE_TOKENS = 20
MAX_TOKEN_NAME_LENGTH = 40
LAST_USED_RESOLUTION = timedelta(minutes=1)
MAX_AUDIT_SUMMARY_LENGTH = 200
MAX_ACTIVITY_LIMIT = 200

READ_SCOPE = "read"
WRITE_SCOPE = "write"


class AgentAccessError(Exception):
    status_code = 400
    code = "agent_access_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        self.message = message
        self.code = code or type(self).code
        super().__init__(message)


class AgentTokenNotFoundError(AgentAccessError):
    status_code = 404
    code = "not_found"


class AgentTokenLimitError(AgentAccessError):
    status_code = 409
    code = "token_limit"


class AgentTokenValidationError(AgentAccessError):
    status_code = 422
    code = "validation_error"


@dataclass(frozen=True, slots=True)
class AgentIdentity:
    """Who an MCP call acts for. Resolved from the bearer token only."""

    user_id: str
    token_id: str
    token_name: str
    scopes: frozenset[str]

    @property
    def can_write(self) -> bool:
        return WRITE_SCOPE in self.scopes


@dataclass(frozen=True, slots=True)
class AuditEntry:
    entry: AgentAuditLog
    token_name: str


def stored_scopes(*, write: bool) -> str:
    return "read write" if write else "read"


def scope_list(stored: str) -> list[str]:
    scopes = set(stored.split())
    # ``write`` implies ``read`` even if a row was ever stored without it.
    if WRITE_SCOPE in scopes:
        scopes.add(READ_SCOPE)
    return [scope for scope in (READ_SCOPE, WRITE_SCOPE) if scope in scopes]


def new_agent_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def normalize_token_name(value: str) -> str:
    name = " ".join(unicodedata.normalize("NFKC", value).split())
    if not name:
        raise AgentTokenValidationError("令牌名称不能为空")
    if any(not character.isprintable() for character in name):
        raise AgentTokenValidationError("令牌名称包含不可见字符")
    if len(name) > MAX_TOKEN_NAME_LENGTH:
        raise AgentTokenValidationError(f"令牌名称不能超过 {MAX_TOKEN_NAME_LENGTH} 个字符")
    return name


def _as_utc(value: datetime) -> datetime:
    # SQLite hands back naive UTC wall-clock values.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def issue_token(
    session: AsyncSession,
    user_id: str,
    *,
    name: str,
    write: bool,
) -> tuple[AgentToken, str]:
    """Create one token; the returned plaintext is never retrievable again."""

    display_name = normalize_token_name(name)
    # The account row is the per-account writer mutex used elsewhere; it makes
    # "count active, then insert" atomic against a concurrent create.
    if not await reserve_account_taxonomy(session, user_id):
        raise AgentTokenNotFoundError("账号不存在")
    active = int(
        await session.scalar(
            select(func.count(AgentToken.id)).where(
                AgentToken.user_id == user_id,
                AgentToken.revoked_at.is_(None),
            )
        )
        or 0
    )
    if active >= MAX_ACTIVE_TOKENS:
        await session.rollback()
        raise AgentTokenLimitError(
            f"最多同时保留 {MAX_ACTIVE_TOKENS} 个有效令牌，请先吊销不再使用的令牌"
        )
    plaintext = new_agent_token()
    token = AgentToken(
        user_id=user_id,
        name=display_name,
        token_hash=token_hash(plaintext),
        token_prefix=plaintext[:DISPLAY_PREFIX_LENGTH],
        scopes=stored_scopes(write=write),
    )
    session.add(token)
    await session.commit()
    return token, plaintext


async def list_tokens(session: AsyncSession, user_id: str) -> list[AgentToken]:
    """Active tokens, newest first. Revoked rows stay only for the audit log."""

    return list(
        (
            await session.scalars(
                select(AgentToken)
                .where(AgentToken.user_id == user_id, AgentToken.revoked_at.is_(None))
                .order_by(AgentToken.created_at.desc(), AgentToken.id.desc())
            )
        ).all()
    )


async def revoke_token(session: AsyncSession, user_id: str, token_id: str) -> None:
    """Revoke one of the account's tokens. Repeating a revoke is a no-op."""

    token = await session.scalar(
        select(AgentToken).where(AgentToken.user_id == user_id, AgentToken.id == token_id)
    )
    if token is None:
        raise AgentTokenNotFoundError("令牌不存在")
    if token.revoked_at is None:
        token.revoked_at = utc_now()
        await session.commit()


async def resolve_token(
    session: AsyncSession,
    raw_token: str,
    *,
    now: datetime | None = None,
) -> AgentIdentity | None:
    """Map a presented bearer token to its identity, or ``None``.

    Unknown, revoked, malformed and inactive-account tokens all look the same
    to the caller. A successful lookup refreshes ``last_used_at`` at most once
    per ``LAST_USED_RESOLUTION``.
    """

    if (
        not raw_token.startswith(TOKEN_PREFIX)
        or len(raw_token) > MAX_TOKEN_LENGTH
        or any(character.isspace() for character in raw_token)
    ):
        return None
    row = (
        await session.execute(
            select(AgentToken)
            .join(User, User.id == AgentToken.user_id)
            .where(
                AgentToken.token_hash == token_hash(raw_token),
                AgentToken.revoked_at.is_(None),
                User.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    current = now or utc_now()
    if row.last_used_at is None or _as_utc(row.last_used_at) <= current - LAST_USED_RESOLUTION:
        # Conditional so concurrent calls of one token write once.
        await session.execute(
            update(AgentToken)
            .where(
                AgentToken.id == row.id,
                or_(
                    AgentToken.last_used_at.is_(None),
                    AgentToken.last_used_at <= current - LAST_USED_RESOLUTION,
                ),
            )
            .values(last_used_at=current)
            .execution_options(synchronize_session=False)
        )
        await session.commit()
    return AgentIdentity(
        user_id=row.user_id,
        token_id=row.id,
        token_name=row.name,
        scopes=frozenset(scope_list(row.scopes)),
    )


def _summary(text: str) -> str:
    collapsed = " ".join(text.split()) or "（无摘要）"
    if len(collapsed) <= MAX_AUDIT_SUMMARY_LENGTH:
        return collapsed
    return collapsed[: MAX_AUDIT_SUMMARY_LENGTH - 1] + "…"


async def record_write(
    session: AsyncSession,
    identity: AgentIdentity,
    *,
    tool: str,
    site_id: str | None,
    summary: str,
) -> None:
    """Append one audit row for a successful MCP write."""

    session.add(
        AgentAuditLog(
            user_id=identity.user_id,
            token_id=identity.token_id,
            tool=tool,
            site_id=site_id,
            summary=_summary(summary),
        )
    )
    await session.commit()


async def list_activity(
    session: AsyncSession,
    user_id: str,
    *,
    limit: int,
) -> list[AuditEntry]:
    bounded = max(1, min(limit, MAX_ACTIVITY_LIMIT))
    rows = (
        await session.execute(
            select(AgentAuditLog, AgentToken.name)
            .join(
                AgentToken,
                (AgentToken.user_id == AgentAuditLog.user_id)
                & (AgentToken.id == AgentAuditLog.token_id),
            )
            .where(AgentAuditLog.user_id == user_id)
            .order_by(AgentAuditLog.created_at.desc(), AgentAuditLog.id.desc())
            .limit(bounded)
        )
    ).all()
    return [AuditEntry(entry, name) for entry, name in rows]
