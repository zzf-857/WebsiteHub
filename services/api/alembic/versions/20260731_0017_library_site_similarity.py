"""Persist account-scoped library similarity review snapshots.

Revision ID: 20260731_0017
Revises: 20260731_0016
Create Date: 2026-07-31
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260731_0017"
down_revision: str | None = "20260731_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "site_similarity_scan_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("ruleset_version", sa.String(length=64), nullable=False),
        sa.Column("library_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("site_count", sa.Integer(), nullable=False),
        sa.Column("duplicate_group_count", sa.Integer(), nullable=False),
        sa.Column("same_site_group_count", sa.Integer(), nullable=False),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('ready', 'applied', 'superseded')",
            name=op.f("ck_site_similarity_scan_runs_valid_status"),
        ),
        sa.CheckConstraint(
            "length(ruleset_version) BETWEEN 1 AND 64",
            name=op.f("ck_site_similarity_scan_runs_valid_ruleset_version"),
        ),
        sa.CheckConstraint(
            "length(library_fingerprint) = 64",
            name=op.f("ck_site_similarity_scan_runs_valid_library_fingerprint"),
        ),
        sa.CheckConstraint(
            "site_count >= 0", name=op.f("ck_site_similarity_scan_runs_nonnegative_site_count")
        ),
        sa.CheckConstraint(
            "duplicate_group_count >= 0",
            name=op.f("ck_site_similarity_scan_runs_nonnegative_duplicate_group_count"),
        ),
        sa.CheckConstraint(
            "same_site_group_count >= 0",
            name=op.f("ck_site_similarity_scan_runs_nonnegative_same_site_group_count"),
        ),
        sa.CheckConstraint(
            "member_count >= 0",
            name=op.f("ck_site_similarity_scan_runs_nonnegative_member_count"),
        ),
        sa.CheckConstraint(
            "version > 0", name=op.f("ck_site_similarity_scan_runs_positive_version")
        ),
        sa.CheckConstraint(
            "(status = 'applied' AND applied_at IS NOT NULL AND result_json IS NOT NULL) OR "
            "(status != 'applied' AND applied_at IS NULL AND result_json IS NULL)",
            name=op.f("ck_site_similarity_scan_runs_applied_result_consistency"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_site_similarity_scan_runs_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_site_similarity_scan_runs")),
        sa.UniqueConstraint(
            "user_id",
            "id",
            name="site_similarity_run_account_identity",
        ),
    )
    op.create_index(
        "uq_site_similarity_ready_run_per_user",
        "site_similarity_scan_runs",
        ["user_id"],
        unique=True,
        sqlite_where=sa.text("status = 'ready'"),
        postgresql_where=sa.text("status = 'ready'"),
    )
    op.create_index(
        "ix_site_similarity_runs_user_created_id",
        "site_similarity_scan_runs",
        ["user_id", "created_at", "id"],
        unique=False,
    )

    op.create_table(
        "site_similarity_groups",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("site_key", sa.String(length=320), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("display_host", sa.String(length=320), nullable=False),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("recommended_site_id", sa.String(length=36), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('duplicate', 'same_site')",
            name=op.f("ck_site_similarity_groups_valid_kind"),
        ),
        sa.CheckConstraint(
            "member_count >= 2", name=op.f("ck_site_similarity_groups_minimum_member_count")
        ),
        sa.CheckConstraint(
            "ordinal >= 0", name=op.f("ck_site_similarity_groups_nonnegative_ordinal")
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "run_id"],
            ["site_similarity_scan_runs.user_id", "site_similarity_scan_runs.id"],
            name=op.f("fk_site_similarity_groups_site_similarity_group_run_same_account"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_site_similarity_groups")),
        sa.UniqueConstraint(
            "user_id",
            "run_id",
            "id",
            name="site_similarity_group_run_identity",
        ),
        sa.UniqueConstraint(
            "user_id",
            "run_id",
            "ordinal",
            name="site_similarity_group_ordinal_per_run",
        ),
    )
    op.create_index(
        "ix_site_similarity_groups_user_run_kind_ordinal",
        "site_similarity_groups",
        ["user_id", "run_id", "kind", "ordinal"],
        unique=False,
    )

    op.create_table(
        "site_similarity_group_members",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("group_id", sa.String(length=36), nullable=False),
        sa.Column("site_id", sa.String(length=36), nullable=False),
        sa.Column("expected_version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("original_url", sa.Text(), nullable=False),
        sa.Column("identity_url", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("favicon_url", sa.Text(), nullable=True),
        sa.Column("preview_url", sa.Text(), nullable=True),
        sa.Column("category_id", sa.String(length=36), nullable=False),
        sa.Column("category_name", sa.String(length=80), nullable=False),
        sa.Column("category_is_default", sa.Boolean(), nullable=False),
        sa.Column("category_icon", sa.String(length=32), nullable=False),
        sa.Column("tags_json", sa.Text(), nullable=False),
        sa.Column("pinned", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("analysis_status", sa.String(length=32), nullable=False),
        sa.Column("site_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("site_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("is_recommended", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "expected_version > 0",
            name=op.f("ck_site_similarity_group_members_positive_expected_version"),
        ),
        sa.CheckConstraint(
            "sort_order >= 0",
            name=op.f("ck_site_similarity_group_members_nonnegative_sort_order"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "run_id", "group_id"],
            [
                "site_similarity_groups.user_id",
                "site_similarity_groups.run_id",
                "site_similarity_groups.id",
            ],
            name=op.f("fk_site_similarity_group_members_site_similarity_member_group_same_run"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "user_id",
            "run_id",
            "group_id",
            "site_id",
            name=op.f("pk_site_similarity_group_members"),
        ),
    )
    op.create_index(
        "ix_site_similarity_members_user_run_group_order",
        "site_similarity_group_members",
        ["user_id", "run_id", "group_id", "sort_order"],
        unique=False,
    )

    op.create_table(
        "site_similarity_decisions",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("group_id", sa.String(length=36), nullable=False),
        sa.Column("keep_site_id", sa.String(length=36), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id", "run_id", "group_id"],
            [
                "site_similarity_groups.user_id",
                "site_similarity_groups.run_id",
                "site_similarity_groups.id",
            ],
            name=op.f("fk_site_similarity_decisions_site_similarity_decision_group_same_run"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "user_id", "run_id", "group_id", name=op.f("pk_site_similarity_decisions")
        ),
    )


def downgrade() -> None:
    op.drop_table("site_similarity_decisions")
    op.drop_index(
        "ix_site_similarity_members_user_run_group_order",
        table_name="site_similarity_group_members",
    )
    op.drop_table("site_similarity_group_members")
    op.drop_index(
        "ix_site_similarity_groups_user_run_kind_ordinal",
        table_name="site_similarity_groups",
    )
    op.drop_table("site_similarity_groups")
    op.drop_index(
        "ix_site_similarity_runs_user_created_id",
        table_name="site_similarity_scan_runs",
    )
    op.drop_index(
        "uq_site_similarity_ready_run_per_user",
        table_name="site_similarity_scan_runs",
    )
    op.drop_table("site_similarity_scan_runs")
