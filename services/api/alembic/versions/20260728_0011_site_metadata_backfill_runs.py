"""Persist metadata-backfill runs and their fixed target snapshots.

Revision ID: 20260728_0011
Revises: 20260728_0010
Create Date: 2026-07-28
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260728_0011"
down_revision: str | None = "20260728_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # New tables only. Do not use SQLite batch mode or alter `sites`: its FTS
    # triggers make table rebuilds fragile, and these job records do not need
    # one.
    op.create_table(
        "site_metadata_preferences",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("site_id", sa.String(length=36), nullable=False),
        sa.Column("description_is_manual", sa.Boolean(), nullable=False),
        sa.Column("favicon_is_manual", sa.Boolean(), nullable=False),
        sa.Column("category_is_manual", sa.Boolean(), nullable=False),
        sa.Column("tags_are_manual", sa.Boolean(), nullable=False),
        sa.Column("description_is_llm", sa.Boolean(), nullable=False),
        sa.Column("category_is_llm", sa.Boolean(), nullable=False),
        sa.Column("tags_are_llm", sa.Boolean(), nullable=False),
        sa.Column("preview_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("llm_analyzed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id", "site_id"],
            ["sites.user_id", "sites.id"],
            name="site_metadata_preference_site_same_account",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "site_id"),
    )
    op.create_table(
        "site_metadata_backfill_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("total_count", sa.Integer(), nullable=False),
        sa.Column("queued_count", sa.Integer(), nullable=False),
        sa.Column("running_count", sa.Integer(), nullable=False),
        sa.Column("complete_count", sa.Integer(), nullable=False),
        sa.Column("limited_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("skipped_count", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("lease_token_hash", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stop_requested", sa.Boolean(), nullable=False),
        sa.Column("consecutive_provider_failures", sa.Integer(), nullable=False),
        sa.Column("provider_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('queued', 'running', 'completed', "
            "'completed_with_errors', 'failed')",
            name="valid_state",
        ),
        sa.CheckConstraint("version > 0", name="positive_version"),
        sa.CheckConstraint(
            "consecutive_provider_failures >= 0",
            name="nonnegative_provider_failures",
        ),
        sa.CheckConstraint("total_count >= 0", name="nonnegative_total_count"),
        sa.CheckConstraint(
            "queued_count >= 0 AND running_count >= 0 AND complete_count >= 0 "
            "AND limited_count >= 0 AND failed_count >= 0 AND skipped_count >= 0",
            name="nonnegative_progress_counts",
        ),
        sa.CheckConstraint(
            "queued_count + running_count + complete_count + limited_count "
            "+ failed_count + skipped_count = total_count",
            name="progress_counts_match_total",
        ),
        sa.CheckConstraint(
            "(state IN ('queued', 'running') AND completed_at IS NULL) OR "
            "(state IN ('completed', 'completed_with_errors', 'failed') "
            "AND completed_at IS NOT NULL)",
            name="terminal_completion_time",
        ),
        sa.CheckConstraint(
            "state != 'running' OR lease_expires_at IS NOT NULL",
            name="running_has_lease",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="site_metadata_backfill_run_user_owner",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "id",
            name="site_metadata_backfill_run_account_identity",
        ),
    )
    op.create_index(
        "uq_site_metadata_backfill_runs_active_per_user",
        "site_metadata_backfill_runs",
        ["user_id"],
        unique=True,
        sqlite_where=sa.text("state IN ('queued', 'running')"),
        postgresql_where=sa.text("state IN ('queued', 'running')"),
    )
    op.create_index(
        "ix_site_metadata_backfill_runs_user_state_updated_id",
        "site_metadata_backfill_runs",
        ["user_id", "state", "updated_at", "id"],
    )
    op.create_index(
        "ix_site_metadata_backfill_runs_lease_expiry",
        "site_metadata_backfill_runs",
        ["state", "lease_expires_at", "id"],
    )

    op.create_table(
        "site_metadata_backfill_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("site_id", sa.String(length=36), nullable=False),
        sa.Column("expected_version", sa.Integer(), nullable=False),
        sa.Column("initial_analysis_status", sa.String(length=32), nullable=False),
        sa.Column("requires_llm", sa.Boolean(), nullable=False),
        sa.Column("origin_key", sa.String(length=320), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("analysis_claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_token_hash", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('queued', 'running', 'complete', 'limited', 'failed', 'skipped')",
            name="valid_state",
        ),
        sa.CheckConstraint(
            "initial_analysis_status IN "
            "('not_analyzed', 'pending', 'complete', 'failed', 'limited')",
            name="valid_initial_analysis_status",
        ),
        sa.CheckConstraint("expected_version > 0", name="positive_expected_version"),
        sa.CheckConstraint("attempt_count >= 0", name="nonnegative_attempt_count"),
        sa.CheckConstraint(
            "length(origin_key) BETWEEN 1 AND 320",
            name="valid_origin_key_length",
        ),
        sa.CheckConstraint(
            "(state IN ('queued', 'running') AND completed_at IS NULL) OR "
            "(state IN ('complete', 'limited', 'failed', 'skipped') "
            "AND completed_at IS NOT NULL)",
            name="terminal_completion_time",
        ),
        sa.CheckConstraint(
            "state != 'running' OR lease_expires_at IS NOT NULL",
            name="running_has_lease",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "run_id"],
            [
                "site_metadata_backfill_runs.user_id",
                "site_metadata_backfill_runs.id",
            ],
            name="site_metadata_backfill_item_run_same_account",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "id",
            name="site_metadata_backfill_item_account_identity",
        ),
        sa.UniqueConstraint(
            "user_id",
            "run_id",
            "site_id",
            name="site_metadata_backfill_item_once_per_run",
        ),
    )
    op.create_index(
        "ix_site_metadata_backfill_items_user_run_state_created_id",
        "site_metadata_backfill_items",
        ["user_id", "run_id", "state", "created_at", "id"],
    )
    op.create_index(
        "ix_site_metadata_backfill_items_user_state_lease_id",
        "site_metadata_backfill_items",
        ["user_id", "state", "lease_expires_at", "id"],
    )
    op.create_index(
        "ix_site_metadata_backfill_items_user_origin_state_id",
        "site_metadata_backfill_items",
        ["user_id", "origin_key", "state", "id"],
    )
    op.create_index(
        "uq_site_metadata_backfill_items_running_origin_per_run",
        "site_metadata_backfill_items",
        ["user_id", "run_id", "origin_key"],
        unique=True,
        sqlite_where=sa.text("state = 'running'"),
        postgresql_where=sa.text("state = 'running'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_site_metadata_backfill_items_running_origin_per_run",
        table_name="site_metadata_backfill_items",
    )
    op.drop_index(
        "ix_site_metadata_backfill_items_user_origin_state_id",
        table_name="site_metadata_backfill_items",
    )
    op.drop_index(
        "ix_site_metadata_backfill_items_user_state_lease_id",
        table_name="site_metadata_backfill_items",
    )
    op.drop_index(
        "ix_site_metadata_backfill_items_user_run_state_created_id",
        table_name="site_metadata_backfill_items",
    )
    op.drop_table("site_metadata_backfill_items")

    op.drop_index(
        "ix_site_metadata_backfill_runs_lease_expiry",
        table_name="site_metadata_backfill_runs",
    )
    op.drop_index(
        "ix_site_metadata_backfill_runs_user_state_updated_id",
        table_name="site_metadata_backfill_runs",
    )
    op.drop_index(
        "uq_site_metadata_backfill_runs_active_per_user",
        table_name="site_metadata_backfill_runs",
    )
    op.drop_table("site_metadata_backfill_runs")
    op.drop_table("site_metadata_preferences")
