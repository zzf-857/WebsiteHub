"""External Agent access: MCP tokens and the write audit log (Q43 / 2026-10 D3)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from webhub.db.models._base import Base, new_id, utc_now

# Stored scope sets. ``write`` always implies ``read``; only these two
# combinations exist, so the column is a closed enumeration, not free text.
AGENT_TOKEN_SCOPES = ("read", "read write")


class AgentToken(Base):
    """One revocable bearer token an external Agent uses to reach MCP.

    Only the SHA-256 digest of the token is stored (like login sessions); the
    plaintext is shown once at creation. ``token_prefix`` is a short display
    fragment so the user can tell tokens apart. Rows are never deleted on
    revoke, so audit entries keep resolving to a token name.
    """

    __tablename__ = "agent_tokens"
    __table_args__ = (
        UniqueConstraint("user_id", "id", name="agent_token_account_identity"),
        CheckConstraint("length(name) BETWEEN 1 AND 40", name="valid_agent_token_name"),
        CheckConstraint("length(token_hash) = 64", name="valid_agent_token_hash"),
        CheckConstraint("scopes IN ('read', 'read write')", name="valid_agent_token_scopes"),
        Index("ix_agent_tokens_user_revoked_created", "user_id", "revoked_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(40), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    token_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    scopes: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentAuditLog(Base):
    """One successful MCP write, shown to the user on /settings/agents.

    ``site_id`` is deliberately not a foreign key: the history of what an
    Agent did must survive the user deleting that resource later.
    """

    __tablename__ = "agent_audit_log"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "token_id"],
            ["agent_tokens.user_id", "agent_tokens.id"],
            name="agent_audit_token_same_account",
            ondelete="CASCADE",
        ),
        CheckConstraint("length(summary) BETWEEN 1 AND 200", name="valid_agent_audit_summary"),
        Index("ix_agent_audit_log_user_created_id", "user_id", "created_at", "id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_id: Mapped[str] = mapped_column(String(36), nullable=False)
    tool: Mapped[str] = mapped_column(String(64), nullable=False)
    site_id: Mapped[str | None] = mapped_column(String(36))
    summary: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
