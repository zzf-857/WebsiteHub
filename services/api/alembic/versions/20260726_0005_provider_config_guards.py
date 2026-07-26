"""Add Provider optimistic locking and single-enabled guards.

Revision ID: 20260726_0005
Revises: 20260726_0004
Create Date: 2026-07-26
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260726_0005"
down_revision: str | None = "20260726_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "provider_configs",
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.execute(
        sa.text(
            "UPDATE provider_configs AS candidate "
            "SET enabled = 0 "
            "WHERE candidate.enabled = 1 AND EXISTS ("
            "SELECT 1 FROM provider_configs AS winner "
            "WHERE winner.user_id = candidate.user_id "
            "AND winner.kind = candidate.kind "
            "AND winner.enabled = 1 "
            "AND (winner.updated_at > candidate.updated_at "
            "OR (winner.updated_at = candidate.updated_at AND winner.id > candidate.id))"
            ")"
        )
    )
    op.create_index(
        "uq_provider_configs_enabled_per_user_kind",
        "provider_configs",
        ["user_id", "kind"],
        unique=True,
        sqlite_where=sa.text("enabled = 1"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_provider_configs_enabled_per_user_kind",
        table_name="provider_configs",
    )
    op.drop_column("provider_configs", "version")
