"""Allow multiple kept members in one library similarity decision.

Revision ID: 20260731_0018
Revises: 20260731_0017
Create Date: 2026-07-31
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260731_0018"
down_revision: str | None = "20260731_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "site_similarity_decision_members",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("group_id", sa.String(length=36), nullable=False),
        sa.Column("site_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id", "run_id", "group_id"],
            [
                "site_similarity_decisions.user_id",
                "site_similarity_decisions.run_id",
                "site_similarity_decisions.group_id",
            ],
            name=op.f(
                "fk_site_similarity_decision_members_"
                "site_similarity_selected_decision_same_run"
            ),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "run_id", "group_id", "site_id"],
            [
                "site_similarity_group_members.user_id",
                "site_similarity_group_members.run_id",
                "site_similarity_group_members.group_id",
                "site_similarity_group_members.site_id",
            ],
            name=op.f(
                "fk_site_similarity_decision_members_"
                "site_similarity_selected_member_same_group"
            ),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "user_id",
            "run_id",
            "group_id",
            "site_id",
            name=op.f("pk_site_similarity_decision_members"),
        ),
    )
    op.execute(
        sa.text(
            "INSERT INTO site_similarity_decision_members "
            "(user_id, run_id, group_id, site_id) "
            "SELECT decisions.user_id, decisions.run_id, decisions.group_id, "
            "decisions.keep_site_id "
            "FROM site_similarity_decisions AS decisions "
            "JOIN site_similarity_group_members AS members "
            "ON members.user_id = decisions.user_id "
            "AND members.run_id = decisions.run_id "
            "AND members.group_id = decisions.group_id "
            "AND members.site_id = decisions.keep_site_id "
            "WHERE decisions.keep_site_id IS NOT NULL"
        )
    )
    # 0017 did not constrain keep_site_id to the selected group. Preserve safety
    # when upgrading any historical bad row by treating it as "keep all".
    op.execute(
        sa.text(
            "UPDATE site_similarity_decisions AS decisions SET keep_site_id = NULL "
            "WHERE decisions.keep_site_id IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM site_similarity_decision_members AS members "
            "WHERE members.user_id = decisions.user_id "
            "AND members.run_id = decisions.run_id "
            "AND members.group_id = decisions.group_id "
            "AND members.site_id = decisions.keep_site_id)"
        )
    )


def downgrade() -> None:
    # Old code can represent one keeper or all keep. Multiple keepers therefore
    # degrade to all keep, which is the only non-destructive fallback.
    op.execute(
        sa.text(
            "UPDATE site_similarity_decisions AS decisions "
            "SET keep_site_id = CASE "
            "WHEN (SELECT COUNT(*) FROM site_similarity_decision_members AS members "
            "WHERE members.user_id = decisions.user_id "
            "AND members.run_id = decisions.run_id "
            "AND members.group_id = decisions.group_id) = 1 "
            "THEN (SELECT site_id FROM site_similarity_decision_members AS members "
            "WHERE members.user_id = decisions.user_id "
            "AND members.run_id = decisions.run_id "
            "AND members.group_id = decisions.group_id LIMIT 1) "
            "ELSE NULL END"
        )
    )
    op.drop_table("site_similarity_decision_members")
