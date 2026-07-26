from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from webhub.db.base import Base
from webhub.db.models import new_id, utc_now

MAX_CONVERSATION_TITLE_LENGTH = 160
MAX_MESSAGE_CONTENT_LENGTH = 200_000
MAX_MESSAGE_JSON_BYTES = 512 * 1024


class Conversation(Base):
    """A user-owned conversation and its optimistic-locking state."""

    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("user_id", "id", name="conversation_account_identity"),
        CheckConstraint(
            f"length(title) BETWEEN 1 AND {MAX_CONVERSATION_TITLE_LENGTH}",
            name="valid_conversation_title_length",
        ),
        CheckConstraint("version > 0", name="positive_conversation_version"),
        CheckConstraint("message_count >= 0", name="nonnegative_message_count"),
        Index(
            "ix_conversations_user_last_message_id",
            "user_id",
            "last_message_at",
            "id",
        ),
        Index("ix_conversations_user_created_id", "user_id", "created_at", "id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(
        String(MAX_CONVERSATION_TITLE_LENGTH), nullable=False, default="新会话"
    )
    title_is_custom: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_message_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class ConversationMessage(Base):
    """A replayable message with bounded JSON sidecars.

    The composite conversation foreign key is deliberate: a message cannot be
    attached to a conversation owned by another account even if an ID is guessed.
    """

    __tablename__ = "conversation_messages"
    __table_args__ = (
        UniqueConstraint("user_id", "id", name="conversation_message_account_identity"),
        UniqueConstraint(
            "user_id",
            "conversation_id",
            "sequence",
            name="conversation_message_sequence_per_account",
        ),
        ForeignKeyConstraint(
            ["user_id", "conversation_id"],
            ["conversations.user_id", "conversations.id"],
            name="conversation_message_conversation_same_account",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "role IN ('system', 'user', 'assistant', 'tool')",
            name="valid_conversation_message_role",
        ),
        CheckConstraint(
            "status IN ('streaming', 'complete', 'error', 'aborted')",
            name="valid_conversation_message_status",
        ),
        CheckConstraint("sequence > 0", name="positive_conversation_message_sequence"),
        CheckConstraint("version > 0", name="positive_conversation_message_version"),
        CheckConstraint(
            f"length(content) <= {MAX_MESSAGE_CONTENT_LENGTH}",
            name="conversation_message_content_size",
        ),
        CheckConstraint(
            f"length(parts_json) <= {MAX_MESSAGE_JSON_BYTES}",
            name="conversation_message_parts_size",
        ),
        CheckConstraint(
            f"length(sources_json) <= {MAX_MESSAGE_JSON_BYTES}",
            name="conversation_message_sources_size",
        ),
        CheckConstraint(
            f"length(artifacts_json) <= {MAX_MESSAGE_JSON_BYTES}",
            name="conversation_message_artifacts_size",
        ),
        CheckConstraint(
            f"length(metadata_json) <= {MAX_MESSAGE_JSON_BYTES}",
            name="conversation_message_metadata_size",
        ),
        CheckConstraint(
            "idempotency_key_hash IS NULL OR length(idempotency_key_hash) = 64",
            name="valid_conversation_message_idempotency_hash",
        ),
        CheckConstraint(
            "length(payload_hash) = 64", name="valid_conversation_message_payload_hash"
        ),
        Index(
            "ix_conversation_messages_user_conversation_sequence",
            "user_id",
            "conversation_id",
            "sequence",
        ),
        Index(
            "ix_conversation_messages_user_created_id",
            "user_id",
            "created_at",
            "id",
        ),
        Index(
            "uq_conversation_messages_idempotency_per_account",
            "user_id",
            "conversation_id",
            "idempotency_key_hash",
            unique=True,
            sqlite_where=text("idempotency_key_hash IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    parts_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    sources_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    artifacts_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="complete")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    idempotency_key_hash: Mapped[str | None] = mapped_column(String(64))
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )
