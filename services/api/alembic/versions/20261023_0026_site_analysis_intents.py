"""Persist outstanding new-site analysis requests.

Revision ID: 20261023_0026
Revises: 20261023_0025
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261023_0026"
down_revision = "20261023_0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "site_analysis_intents",
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "site_id",
            sa.String(36),
            sa.ForeignKey("sites.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("intent", sa.String(32), nullable=False, server_default="site_enrichment"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id", "site_id"],
            ["sites.user_id", "sites.id"],
            name="site_analysis_intent_site_same_account",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("intent IN ('site_enrichment')", name="site_analysis_intent_kind"),
    )
    op.create_index(
        "ix_site_analysis_intents_next_attempt_at", "site_analysis_intents", ["next_attempt_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_site_analysis_intents_next_attempt_at", table_name="site_analysis_intents")
    op.drop_table("site_analysis_intents")
