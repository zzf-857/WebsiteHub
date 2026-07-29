"""Durable, account-scoped execution ledger for Agent chat turns."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from webhub.db.models._base import Base, new_id, utc_now

MAX_AGENT_TURN_ID_LENGTH = 128
MAX_AGENT_TURN_ERROR_CODE_LENGTH = 64


class AgentTurnRun(Base):
    """One idempotent Agent execution owned by one account.

    The browser's raw turn id and the random lease capability are never stored.
    Their SHA-256 digests are sufficient for equality and fencing checks while
    keeping database copies from becoming reusable credentials.
    """

    __tablename__ = "agent_turn_runs"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "turn_id_hash",
            name="agent_turn_run_per_account",
        ),
        CheckConstraint(
            "state IN ('running', 'complete', 'error', 'aborted')",
            name="valid_agent_turn_state",
        ),
        CheckConstraint("length(turn_id_hash) = 64", name="valid_agent_turn_id_hash"),
        CheckConstraint("length(request_hash) = 64", name="valid_agent_turn_request_hash"),
        CheckConstraint("attempt_count > 0", name="positive_agent_turn_attempt_count"),
        CheckConstraint(
            "lease_token_hash IS NULL OR length(lease_token_hash) = 64",
            name="valid_agent_turn_lease_hash",
        ),
        CheckConstraint(
            "(state = 'running' AND lease_token_hash IS NOT NULL "
            "AND lease_expires_at IS NOT NULL AND completed_at IS NULL) OR "
            "(state != 'running' AND lease_token_hash IS NULL "
            "AND lease_expires_at IS NULL AND completed_at IS NOT NULL)",
            name="valid_agent_turn_lifecycle",
        ),
        ForeignKeyConstraint(
            ["user_id", "requested_conversation_id"],
            ["conversations.user_id", "conversations.id"],
            name="agent_turn_run_requested_conversation_same_account",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["user_id", "conversation_id"],
            ["conversations.user_id", "conversations.id"],
            name="agent_turn_run_conversation_same_account",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["user_id", "user_message_id"],
            ["conversation_messages.user_id", "conversation_messages.id"],
            name="agent_turn_run_user_message_same_account",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["user_id", "assistant_message_id"],
            ["conversation_messages.user_id", "conversation_messages.id"],
            name="agent_turn_run_assistant_message_same_account",
            ondelete="CASCADE",
        ),
        Index(
            "ix_agent_turn_runs_state_lease_expiry_id",
            "state",
            "lease_expires_at",
            "id",
        ),
        Index(
            "ix_agent_turn_runs_user_conversation_created_id",
            "user_id",
            "conversation_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    turn_id_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_conversation_id: Mapped[str | None] = mapped_column(String(36))
    conversation_id: Mapped[str | None] = mapped_column(String(36))
    user_message_id: Mapped[str | None] = mapped_column(String(36))
    assistant_message_id: Mapped[str | None] = mapped_column(String(36))
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    lease_token_hash: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(MAX_AGENT_TURN_ERROR_CODE_LENGTH))
    checkpointed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )


__all__ = [
    "MAX_AGENT_TURN_ERROR_CODE_LENGTH",
    "MAX_AGENT_TURN_ID_LENGTH",
    "AgentTurnRun",
]
