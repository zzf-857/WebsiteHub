"""Add deterministic bookmark similarity suggestions and user decisions.

Revision ID: 20260731_0015
Revises: 20260729_0014
Create Date: 2026-07-31
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260731_0015"
down_revision: str | None = "20260729_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _create_projection_freeze_triggers() -> None:
    for table_name in (
        "bookmark_similarity_clusters",
        "bookmark_similarity_cluster_members",
    ):
        for operation in ("INSERT", "UPDATE", "DELETE"):
            references = ("NEW",) if operation == "INSERT" else ("OLD",)
            if operation == "UPDATE":
                references = ("OLD", "NEW")
            terminal_run_checks = " OR ".join(
                f"""EXISTS (
                    SELECT 1 FROM bookmark_import_runs AS run
                    WHERE run.user_id = {reference}.user_id
                      AND run.id = {reference}.run_id
                      AND run.state IN ('complete', 'failed', 'cancelled')
                )"""
                for reference in references
            )
            op.execute(
                f"""
                CREATE TRIGGER {table_name}_terminal_{operation.casefold()}
                BEFORE {operation} ON {table_name}
                WHEN {terminal_run_checks}
                BEGIN
                    SELECT RAISE(ABORT, 'terminal bookmark similarity projection is immutable');
                END
                """
            )


def _drop_projection_freeze_triggers() -> None:
    for table_name in (
        "bookmark_similarity_cluster_members",
        "bookmark_similarity_clusters",
    ):
        for operation in ("delete", "update", "insert"):
            op.execute(f"DROP TRIGGER IF EXISTS {table_name}_terminal_{operation}")


def upgrade() -> None:
    op.create_table(
        "bookmark_similarity_clusters",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("site_key", sa.String(length=255), nullable=False),
        sa.Column("ruleset_version", sa.String(length=64), nullable=False),
        sa.Column("display_host", sa.String(length=255), nullable=False),
        sa.Column("canonical_candidate_id", sa.String(length=36), nullable=True),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("canonical_title", sa.String(length=160), nullable=False),
        sa.Column("canonical_source", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.String(length=16), nullable=False),
        sa.Column("reason_codes_json", sa.Text(), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("occurrence_count", sa.Integer(), nullable=False),
        sa.Column("keep_original_create_count", sa.Integer(), nullable=False),
        sa.Column("merge_create_count", sa.Integer(), nullable=False),
        sa.Column("first_source_sequence", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "candidate_count >= 2",
            name=op.f("ck_bookmark_similarity_clusters_minimum_candidate_count"),
        ),
        sa.CheckConstraint(
            "canonical_source IN ('imported_homepage', 'derived_origin_root', "
            "'existing_library')",
            name=op.f("ck_bookmark_similarity_clusters_valid_canonical_source"),
        ),
        sa.CheckConstraint(
            "confidence IN ('high', 'medium', 'low')",
            name=op.f("ck_bookmark_similarity_clusters_valid_confidence"),
        ),
        sa.CheckConstraint(
            "first_source_sequence > 0",
            name=op.f("ck_bookmark_similarity_clusters_positive_first_sequence"),
        ),
        sa.CheckConstraint(
            "keep_original_create_count >= 0 AND merge_create_count >= 0 "
            "AND merge_create_count <= keep_original_create_count",
            name=op.f("ck_bookmark_similarity_clusters_valid_create_counts"),
        ),
        sa.CheckConstraint(
            "length(canonical_title) BETWEEN 1 AND 160",
            name=op.f("ck_bookmark_similarity_clusters_valid_canonical_title"),
        ),
        sa.CheckConstraint(
            "length(canonical_url) BETWEEN 1 AND 16384",
            name=op.f("ck_bookmark_similarity_clusters_valid_canonical_url"),
        ),
        sa.CheckConstraint(
            "length(display_host) BETWEEN 1 AND 255",
            name=op.f("ck_bookmark_similarity_clusters_valid_display_host"),
        ),
        sa.CheckConstraint(
            "occurrence_count >= candidate_count",
            name=op.f("ck_bookmark_similarity_clusters_valid_occurrence_count"),
        ),
        sa.CheckConstraint(
            "length(ruleset_version) BETWEEN 1 AND 64",
            name=op.f("ck_bookmark_similarity_clusters_valid_ruleset_version"),
        ),
        sa.CheckConstraint(
            "length(site_key) BETWEEN 1 AND 255",
            name=op.f("ck_bookmark_similarity_clusters_valid_site_key"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "run_id", "canonical_candidate_id"],
            [
                "bookmark_staging_candidates.user_id",
                "bookmark_staging_candidates.run_id",
                "bookmark_staging_candidates.id",
            ],
            name="bookmark_similarity_cluster_canonical_candidate_same_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "run_id"],
            ["bookmark_import_runs.user_id", "bookmark_import_runs.id"],
            name="bookmark_similarity_cluster_run_same_account",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bookmark_similarity_clusters")),
        sa.UniqueConstraint(
            "user_id",
            "run_id",
            "id",
            name="bookmark_similarity_cluster_run_identity",
        ),
        sa.UniqueConstraint(
            "user_id",
            "run_id",
            "site_key",
            name="bookmark_similarity_cluster_site_key_per_run",
        ),
    )
    op.create_index(
        "ix_bookmark_similarity_clusters_user_run_sequence_id",
        "bookmark_similarity_clusters",
        ["user_id", "run_id", "first_source_sequence", "id"],
        unique=False,
    )

    op.create_table(
        "bookmark_similarity_cluster_members",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("cluster_id", sa.String(length=36), nullable=False),
        sa.Column("candidate_id", sa.String(length=36), nullable=False),
        sa.Column("first_source_sequence", sa.Integer(), nullable=False),
        sa.Column("is_canonical", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "first_source_sequence > 0",
            name=op.f("ck_bookmark_similarity_cluster_members_positive_first_sequence"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "run_id", "candidate_id"],
            [
                "bookmark_staging_candidates.user_id",
                "bookmark_staging_candidates.run_id",
                "bookmark_staging_candidates.id",
            ],
            name="bookmark_similarity_member_candidate_same_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "run_id", "cluster_id"],
            [
                "bookmark_similarity_clusters.user_id",
                "bookmark_similarity_clusters.run_id",
                "bookmark_similarity_clusters.id",
            ],
            name="bookmark_similarity_member_cluster_same_run",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "user_id",
            "run_id",
            "cluster_id",
            "candidate_id",
            name=op.f("pk_bookmark_similarity_cluster_members"),
        ),
        sa.UniqueConstraint(
            "user_id",
            "run_id",
            "candidate_id",
            name="bookmark_similarity_candidate_once_per_run",
        ),
    )
    op.create_index(
        "ix_bookmark_similarity_members_user_run_cluster_sequence_id",
        "bookmark_similarity_cluster_members",
        ["user_id", "run_id", "cluster_id", "first_source_sequence", "candidate_id"],
        unique=False,
    )

    op.create_table(
        "bookmark_similarity_decision_states",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "version > 0",
            name=op.f("ck_bookmark_similarity_decision_states_positive_version"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "job_id", "run_id"],
            [
                "bookmark_import_runs.user_id",
                "bookmark_import_runs.job_id",
                "bookmark_import_runs.id",
            ],
            name="bookmark_similarity_decision_state_run_same_job",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "user_id",
            "run_id",
            name=op.f("pk_bookmark_similarity_decision_states"),
        ),
        sa.UniqueConstraint(
            "user_id",
            "run_id",
            name="bookmark_similarity_decision_state_run_once",
        ),
    )

    op.create_table(
        "bookmark_similarity_decisions",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("cluster_id", sa.String(length=36), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "decision IN ('merge_to_homepage', 'keep_originals')",
            name=op.f("ck_bookmark_similarity_decisions_valid_decision"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "run_id", "cluster_id"],
            [
                "bookmark_similarity_clusters.user_id",
                "bookmark_similarity_clusters.run_id",
                "bookmark_similarity_clusters.id",
            ],
            name="bookmark_similarity_decision_cluster_same_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "run_id"],
            [
                "bookmark_similarity_decision_states.user_id",
                "bookmark_similarity_decision_states.run_id",
            ],
            name="bookmark_similarity_decision_state_same_run",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "user_id",
            "run_id",
            "cluster_id",
            name=op.f("pk_bookmark_similarity_decisions"),
        ),
    )

    _create_projection_freeze_triggers()


def downgrade() -> None:
    _drop_projection_freeze_triggers()
    op.drop_table("bookmark_similarity_decisions")
    op.drop_table("bookmark_similarity_decision_states")
    op.drop_index(
        "ix_bookmark_similarity_members_user_run_cluster_sequence_id",
        table_name="bookmark_similarity_cluster_members",
    )
    op.drop_table("bookmark_similarity_cluster_members")
    op.drop_index(
        "ix_bookmark_similarity_clusters_user_run_sequence_id",
        table_name="bookmark_similarity_clusters",
    )
    op.drop_table("bookmark_similarity_clusters")
