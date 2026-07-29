"""Add the durable Agent turn execution ledger.

Revision ID: 20260729_0014
Revises: 20260729_0013
Create Date: 2026-07-29
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260729_0014"
down_revision: str | None = "20260729_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_turn_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("turn_id_hash", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("requested_conversation_id", sa.String(length=36), nullable=True),
        sa.Column("conversation_id", sa.String(length=36), nullable=True),
        sa.Column("user_message_id", sa.String(length=36), nullable=True),
        sa.Column("assistant_message_id", sa.String(length=36), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("lease_token_hash", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("checkpointed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('running', 'complete', 'error', 'aborted')",
            name="valid_agent_turn_state",
        ),
        sa.CheckConstraint("length(turn_id_hash) = 64", name="valid_agent_turn_id_hash"),
        sa.CheckConstraint("length(request_hash) = 64", name="valid_agent_turn_request_hash"),
        sa.CheckConstraint("attempt_count > 0", name="positive_agent_turn_attempt_count"),
        sa.CheckConstraint(
            "lease_token_hash IS NULL OR length(lease_token_hash) = 64",
            name="valid_agent_turn_lease_hash",
        ),
        sa.CheckConstraint(
            "(state = 'running' AND lease_token_hash IS NOT NULL "
            "AND lease_expires_at IS NOT NULL AND completed_at IS NULL) OR "
            "(state != 'running' AND lease_token_hash IS NULL "
            "AND lease_expires_at IS NULL AND completed_at IS NOT NULL)",
            name="valid_agent_turn_lifecycle",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["user_id", "requested_conversation_id"],
            ["conversations.user_id", "conversations.id"],
            name="agent_turn_run_requested_conversation_same_account",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "conversation_id"],
            ["conversations.user_id", "conversations.id"],
            name="agent_turn_run_conversation_same_account",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "user_message_id"],
            ["conversation_messages.user_id", "conversation_messages.id"],
            name="agent_turn_run_user_message_same_account",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "assistant_message_id"],
            ["conversation_messages.user_id", "conversation_messages.id"],
            name="agent_turn_run_assistant_message_same_account",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "turn_id_hash",
            name="agent_turn_run_per_account",
        ),
    )
    op.create_index(
        "ix_agent_turn_runs_user_id",
        "agent_turn_runs",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_agent_turn_runs_state_lease_expiry_id",
        "agent_turn_runs",
        ["state", "lease_expires_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_agent_turn_runs_user_conversation_created_id",
        "agent_turn_runs",
        ["user_id", "conversation_id", "created_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_turn_runs_user_conversation_created_id",
        table_name="agent_turn_runs",
    )
    op.drop_index(
        "ix_agent_turn_runs_state_lease_expiry_id",
        table_name="agent_turn_runs",
    )
    op.drop_index("ix_agent_turn_runs_user_id", table_name="agent_turn_runs")
    op.drop_table("agent_turn_runs")
