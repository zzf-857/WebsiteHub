"""Add a per-category custom ordering column to sites.

Revision ID: 20260727_0007
Revises: 20260726_0006
Create Date: 2026-07-27
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260727_0007"
down_revision: str | None = "20260726_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Deliberately *not* `batch_alter_table`: on SQLite that rebuilds the table
    # (create temp → copy → drop → rename), and the FTS triggers that reference
    # `sites` fire during the rename against a table that no longer exists.
    # `ADD COLUMN ... NOT NULL DEFAULT` needs no rebuild, so the triggers are
    # never disturbed.
    op.add_column(
        "sites",
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
    )

    # Backfill in creation order within each (user_id, category_id) bucket, so
    # an account that never touches ordering sees exactly what it saw before.
    op.execute(
        """
        WITH ordered AS (
            SELECT
                id,
                ROW_NUMBER() OVER (
                    PARTITION BY user_id, category_id
                    ORDER BY created_at, id
                ) - 1 AS seq
            FROM sites
        )
        UPDATE sites
        SET position = (SELECT seq FROM ordered WHERE ordered.id = sites.id)
        """
    )

    # A unique *index* rather than a table constraint, for the same
    # no-rebuild reason.  It is what makes "并发重排不会产生重复 position"
    # enforced by the database instead of trusted to the service.
    op.create_index(
        "ix_sites_user_category_position",
        "sites",
        ["user_id", "category_id", "position"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_sites_user_category_position", table_name="sites")
    op.drop_column("sites", "position")
