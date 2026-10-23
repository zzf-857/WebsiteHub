"""Account-scoped backup previews.

Revision ID: 20261023_0027
Revises: 20261022_0024
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261023_0027"
down_revision = "20261022_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "portability_imports",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("payload_path", sa.Text(), nullable=False),
        sa.Column("summary_json", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('previewed', 'applied', 'expired')", name="valid_status"),
        sa.CheckConstraint("length(payload_sha256) = 64", name="valid_payload_sha256"),
    )
    op.create_index(
        "ix_portability_imports_user_created", "portability_imports", ["user_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_portability_imports_user_created", table_name="portability_imports")
    op.drop_table("portability_imports")
