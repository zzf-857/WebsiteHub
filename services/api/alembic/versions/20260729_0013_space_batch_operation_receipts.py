"""Persist idempotent Space batch operation receipts.

Revision ID: 20260729_0013
Revises: 20260729_0012
Create Date: 2026-07-29
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260729_0013"
down_revision: str | None = "20260729_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "space_batch_operation_receipts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("operation_id", sa.String(length=200), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("target_mode", sa.String(length=16), nullable=False),
        sa.Column("target_space_id", sa.String(length=36), nullable=False),
        sa.Column("selected_site_ids_json", sa.Text(), nullable=False),
        sa.Column("result_space_id", sa.String(length=36), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
        sa.Column("added_count", sa.Integer(), nullable=False),
        sa.Column("already_member_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "target_mode IN ('create', 'existing')",
            name="valid_space_batch_target_mode",
        ),
        sa.CheckConstraint(
            "length(payload_hash) = 64",
            name="valid_space_batch_payload_hash",
        ),
        sa.CheckConstraint(
            "added_count >= 0",
            name="nonnegative_space_batch_added_count",
        ),
        sa.CheckConstraint(
            "already_member_count >= 0",
            name="nonnegative_space_batch_existing_count",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "operation_id",
            name="space_batch_operation_per_account",
        ),
    )
    op.create_index(
        "ix_space_batch_operation_receipts_user_id",
        "space_batch_operation_receipts",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_space_batch_receipts_user_result",
        "space_batch_operation_receipts",
        ["user_id", "result_space_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_space_batch_receipts_user_result",
        table_name="space_batch_operation_receipts",
    )
    op.drop_index(
        "ix_space_batch_operation_receipts_user_id",
        table_name="space_batch_operation_receipts",
    )
    op.drop_table("space_batch_operation_receipts")
