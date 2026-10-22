"""Add external Agent (MCP) tokens and the Agent write audit log.

Revision ID: 20261021_0021
Revises: 20261021_0020
Create Date: 2026-10-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261021_0021"
down_revision: str | None = "20261021_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_tokens",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=40), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("token_prefix", sa.String(length=16), nullable=False),
        sa.Column("scopes", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(name) BETWEEN 1 AND 40",
            name=op.f("ck_agent_tokens_valid_agent_token_name"),
        ),
        sa.CheckConstraint(
            "length(token_hash) = 64",
            name=op.f("ck_agent_tokens_valid_agent_token_hash"),
        ),
        sa.CheckConstraint(
            "scopes IN ('read', 'read write')",
            name=op.f("ck_agent_tokens_valid_agent_token_scopes"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_agent_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_agent_tokens_token_hash")),
        sa.UniqueConstraint("user_id", "id", name="agent_token_account_identity"),
    )
    op.create_index(
        "ix_agent_tokens_user_revoked_created",
        "agent_tokens",
        ["user_id", "revoked_at", "created_at"],
        unique=False,
    )
    op.create_table(
        "agent_audit_log",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("token_id", sa.String(length=36), nullable=False),
        sa.Column("tool", sa.String(length=64), nullable=False),
        sa.Column("site_id", sa.String(length=36), nullable=True),
        sa.Column("summary", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(summary) BETWEEN 1 AND 200",
            name=op.f("ck_agent_audit_log_valid_agent_audit_summary"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_agent_audit_log_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "token_id"],
            ["agent_tokens.user_id", "agent_tokens.id"],
            name="agent_audit_token_same_account",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_audit_log")),
    )
    op.create_index(
        "ix_agent_audit_log_user_created_id",
        "agent_audit_log",
        ["user_id", "created_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_agent_audit_log_user_created_id", table_name="agent_audit_log")
    op.drop_table("agent_audit_log")
    op.drop_index("ix_agent_tokens_user_revoked_created", table_name="agent_tokens")
    op.drop_table("agent_tokens")
