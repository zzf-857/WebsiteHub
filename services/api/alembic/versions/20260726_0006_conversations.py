"""Add account-scoped conversations and replayable messages.

Revision ID: 20260726_0006
Revises: 20260726_0005
Create Date: 2026-07-26
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260726_0006"
down_revision: str | None = "20260726_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MAX_TITLE = 160
_MAX_CONTENT = 200_000
_MAX_JSON = 512 * 1024


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("title", sa.String(length=_MAX_TITLE), nullable=False),
        sa.Column("title_is_custom", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("message_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            f"length(title) BETWEEN 1 AND {_MAX_TITLE}",
            name="valid_conversation_title_length",
        ),
        sa.CheckConstraint("version > 0", name="positive_conversation_version"),
        sa.CheckConstraint("message_count >= 0", name="nonnegative_message_count"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "id", name="conversation_account_identity"),
    )
    op.create_index(
        "ix_conversations_user_id",
        "conversations",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_conversations_user_last_message_id",
        "conversations",
        ["user_id", "last_message_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_conversations_user_created_id",
        "conversations",
        ["user_id", "created_at", "id"],
        unique=False,
    )

    op.create_table(
        "conversation_messages",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("parts_json", sa.Text(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("sources_json", sa.Text(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("artifacts_json", sa.Text(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("metadata_json", sa.Text(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default=sa.text("'complete'")
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=True),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "role IN ('system', 'user', 'assistant', 'tool')",
            name="valid_conversation_message_role",
        ),
        sa.CheckConstraint(
            "status IN ('streaming', 'complete', 'error', 'aborted')",
            name="valid_conversation_message_status",
        ),
        sa.CheckConstraint("sequence > 0", name="positive_conversation_message_sequence"),
        sa.CheckConstraint("version > 0", name="positive_conversation_message_version"),
        sa.CheckConstraint(
            f"length(content) <= {_MAX_CONTENT}",
            name="conversation_message_content_size",
        ),
        sa.CheckConstraint(
            f"length(parts_json) <= {_MAX_JSON}",
            name="conversation_message_parts_size",
        ),
        sa.CheckConstraint(
            f"length(sources_json) <= {_MAX_JSON}",
            name="conversation_message_sources_size",
        ),
        sa.CheckConstraint(
            f"length(artifacts_json) <= {_MAX_JSON}",
            name="conversation_message_artifacts_size",
        ),
        sa.CheckConstraint(
            f"length(metadata_json) <= {_MAX_JSON}",
            name="conversation_message_metadata_size",
        ),
        sa.CheckConstraint(
            "idempotency_key_hash IS NULL OR length(idempotency_key_hash) = 64",
            name="valid_conversation_message_idempotency_hash",
        ),
        sa.CheckConstraint(
            "length(payload_hash) = 64",
            name="valid_conversation_message_payload_hash",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["user_id", "conversation_id"],
            ["conversations.user_id", "conversations.id"],
            name="conversation_message_conversation_same_account",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "id", name="conversation_message_account_identity"),
        sa.UniqueConstraint(
            "user_id",
            "conversation_id",
            "sequence",
            name="conversation_message_sequence_per_account",
        ),
    )
    op.create_index(
        "ix_conversation_messages_user_id",
        "conversation_messages",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_conversation_messages_user_conversation_sequence",
        "conversation_messages",
        ["user_id", "conversation_id", "sequence"],
        unique=False,
    )
    op.create_index(
        "ix_conversation_messages_user_created_id",
        "conversation_messages",
        ["user_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "uq_conversation_messages_idempotency_per_account",
        "conversation_messages",
        ["user_id", "conversation_id", "idempotency_key_hash"],
        unique=True,
        sqlite_where=sa.text("idempotency_key_hash IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_conversation_messages_idempotency_per_account",
        table_name="conversation_messages",
    )
    op.drop_index(
        "ix_conversation_messages_user_created_id",
        table_name="conversation_messages",
    )
    op.drop_index(
        "ix_conversation_messages_user_conversation_sequence",
        table_name="conversation_messages",
    )
    op.drop_index("ix_conversation_messages_user_id", table_name="conversation_messages")
    op.drop_table("conversation_messages")
    op.drop_index("ix_conversations_user_created_id", table_name="conversations")
    op.drop_index("ix_conversations_user_last_message_id", table_name="conversations")
    op.drop_index("ix_conversations_user_id", table_name="conversations")
    op.drop_table("conversations")
