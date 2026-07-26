"""Create account-scoped browser bookmark import persistence.

Revision ID: 20260726_0004
Revises: 20260726_0003
Create Date: 2026-07-26
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260726_0004"
down_revision: str | None = "20260726_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUN_CHILD_FREEZE_CONDITIONS = {
    "bookmark_staging_folders": "run.state != 'running'",
    "bookmark_staging_occurrences": "run.state != 'running'",
    "bookmark_staging_candidate_occurrences": "run.state != 'running'",
    "bookmark_staging_candidate_folders": (
        "run.state IN ('complete', 'failed', 'cancelled')"
    ),
}


def _create_terminal_run_child_triggers() -> None:
    for table_name, freeze_condition in _RUN_CHILD_FREEZE_CONDITIONS.items():
        op.execute(
            f"""
            CREATE TRIGGER {table_name}_terminal_insert
            BEFORE INSERT ON {table_name}
            WHEN EXISTS (
                SELECT 1 FROM bookmark_import_runs AS run
                WHERE run.user_id = NEW.user_id
                  AND run.id = NEW.run_id
                  AND {freeze_condition}
            )
            BEGIN
                SELECT RAISE(ABORT, 'terminal bookmark import staging is immutable');
            END
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {table_name}_terminal_update
            BEFORE UPDATE ON {table_name}
            WHEN EXISTS (
                SELECT 1 FROM bookmark_import_runs AS run
                WHERE run.user_id = OLD.user_id
                  AND run.id = OLD.run_id
                  AND {freeze_condition}
            ) OR EXISTS (
                SELECT 1 FROM bookmark_import_runs AS run
                WHERE run.user_id = NEW.user_id
                  AND run.id = NEW.run_id
                  AND {freeze_condition}
            )
            BEGIN
                SELECT RAISE(ABORT, 'terminal bookmark import staging is immutable');
            END
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {table_name}_terminal_delete
            BEFORE DELETE ON {table_name}
            WHEN EXISTS (
                SELECT 1 FROM bookmark_import_runs AS run
                WHERE run.user_id = OLD.user_id
                  AND run.id = OLD.run_id
                  AND {freeze_condition}
            )
            BEGIN
                SELECT RAISE(ABORT, 'terminal bookmark import staging is immutable');
            END
            """
        )

    op.execute(
        """
        CREATE TRIGGER bookmark_import_checkpoints_terminal_parse_insert
        BEFORE INSERT ON bookmark_import_checkpoints
        WHEN NEW.phase = 'parse' AND EXISTS (
            SELECT 1 FROM bookmark_import_runs AS run
            WHERE run.user_id = NEW.user_id
              AND run.id = NEW.run_id
              AND run.state != 'running'
        )
        BEGIN
            SELECT RAISE(ABORT, 'terminal bookmark import parse facts are immutable');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER bookmark_import_checkpoints_terminal_parse_update
        BEFORE UPDATE ON bookmark_import_checkpoints
        WHEN (OLD.phase = 'parse' OR NEW.phase = 'parse') AND (
            EXISTS (
                SELECT 1 FROM bookmark_import_runs AS run
                WHERE run.user_id = OLD.user_id
                  AND run.id = OLD.run_id
                  AND run.state != 'running'
            ) OR EXISTS (
                SELECT 1 FROM bookmark_import_runs AS run
                WHERE run.user_id = NEW.user_id
                  AND run.id = NEW.run_id
                  AND run.state != 'running'
            )
        )
        BEGIN
            SELECT RAISE(ABORT, 'terminal bookmark import parse facts are immutable');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER bookmark_import_checkpoints_terminal_parse_delete
        BEFORE DELETE ON bookmark_import_checkpoints
        WHEN OLD.phase = 'parse' AND EXISTS (
            SELECT 1 FROM bookmark_import_runs AS run
            WHERE run.user_id = OLD.user_id
              AND run.id = OLD.run_id
              AND run.state != 'running'
        )
        BEGIN
            SELECT RAISE(ABORT, 'terminal bookmark import parse facts are immutable');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER bookmark_staging_candidates_terminal_insert
        BEFORE INSERT ON bookmark_staging_candidates
        WHEN EXISTS (
            SELECT 1 FROM bookmark_import_runs AS run
            WHERE run.user_id = NEW.user_id
              AND run.id = NEW.run_id
              AND run.state IN ('finalizing', 'complete', 'failed', 'cancelled')
        )
        BEGIN
            SELECT RAISE(ABORT, 'terminal bookmark import candidate structure is immutable');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER bookmark_staging_candidates_terminal_update
        BEFORE UPDATE ON bookmark_staging_candidates
        WHEN (
            (
                (
                    EXISTS (
                        SELECT 1 FROM bookmark_import_runs AS run
                        WHERE run.user_id = OLD.user_id
                          AND run.id = OLD.run_id
                          AND run.state IN ('complete', 'failed', 'cancelled')
                    ) OR EXISTS (
                        SELECT 1 FROM bookmark_import_runs AS run
                        WHERE run.user_id = NEW.user_id
                          AND run.id = NEW.run_id
                          AND run.state IN ('complete', 'failed', 'cancelled')
                    )
                ) AND (
                    OLD.id IS NOT NEW.id
                    OR OLD.user_id IS NOT NEW.user_id
                    OR OLD.run_id IS NOT NEW.run_id
                    OR OLD.identity_url IS NOT NEW.identity_url
                    OR OLD.identity_hash IS NOT NEW.identity_hash
                    OR OLD.host IS NOT NEW.host
                    OR OLD.fetch_policy IS NOT NEW.fetch_policy
                    OR OLD.has_sensitive_url IS NOT NEW.has_sensitive_url
                    OR OLD.occurrence_count IS NOT NEW.occurrence_count
                    OR OLD.first_source_sequence IS NOT NEW.first_source_sequence
                    OR OLD.created_at IS NOT NEW.created_at
                )
            ) OR (
                (
                    EXISTS (
                        SELECT 1 FROM bookmark_import_runs AS run
                        WHERE run.user_id = OLD.user_id
                          AND run.id = OLD.run_id
                          AND run.state = 'finalizing'
                    ) OR EXISTS (
                        SELECT 1 FROM bookmark_import_runs AS run
                        WHERE run.user_id = NEW.user_id
                          AND run.id = NEW.run_id
                          AND run.state = 'finalizing'
                    )
                ) AND (
                    OLD.id IS NOT NEW.id
                    OR OLD.user_id IS NOT NEW.user_id
                    OR OLD.run_id IS NOT NEW.run_id
                    OR OLD.identity_url IS NOT NEW.identity_url
                    OR OLD.identity_hash IS NOT NEW.identity_hash
                    OR OLD.display_title IS NOT NEW.display_title
                    OR OLD.host IS NOT NEW.host
                    OR OLD.fetch_policy IS NOT NEW.fetch_policy
                    OR OLD.has_sensitive_url IS NOT NEW.has_sensitive_url
                    OR OLD.created_at IS NOT NEW.created_at
                )
            )
        )
        BEGIN
            SELECT RAISE(ABORT, 'terminal bookmark import candidate structure is immutable');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER bookmark_staging_candidates_terminal_delete
        BEFORE DELETE ON bookmark_staging_candidates
        WHEN EXISTS (
            SELECT 1 FROM bookmark_import_runs AS run
            WHERE run.user_id = OLD.user_id
              AND run.id = OLD.run_id
              AND run.state IN ('finalizing', 'complete', 'failed', 'cancelled')
        )
        BEGIN
            SELECT RAISE(ABORT, 'terminal bookmark import candidate structure is immutable');
        END
        """
    )


def _drop_terminal_run_child_triggers() -> None:
    for trigger_name in (
        "bookmark_staging_candidates_terminal_delete",
        "bookmark_staging_candidates_terminal_update",
        "bookmark_staging_candidates_terminal_insert",
        "bookmark_import_checkpoints_terminal_parse_delete",
        "bookmark_import_checkpoints_terminal_parse_update",
        "bookmark_import_checkpoints_terminal_parse_insert",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
    for table_name in _RUN_CHILD_FREEZE_CONDITIONS:
        for operation in ("delete", "update", "insert"):
            op.execute(f"DROP TRIGGER IF EXISTS {table_name}_terminal_{operation}")


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "bookmark_import_snapshots",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_size_bytes", sa.Integer(), nullable=False),
        sa.Column("source_format", sa.String(length=32), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=True),
        sa.Column("storage_key", sa.String(length=255), nullable=False),
        sa.Column("detected_encoding", sa.String(length=40), nullable=True),
        sa.Column("request_idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_format = 'netscape_html'",
            name=op.f("ck_bookmark_import_snapshots_valid_source_format"),
        ),
        sa.CheckConstraint(
            "length(request_idempotency_key_hash) = 64",
            name=op.f("ck_bookmark_import_snapshots_valid_request_idempotency_key_hash"),
        ),
        sa.CheckConstraint(
            "length(source_sha256) = 64",
            name=op.f("ck_bookmark_import_snapshots_valid_source_sha256"),
        ),
        sa.CheckConstraint(
            "source_size_bytes >= 0",
            name=op.f("ck_bookmark_import_snapshots_nonnegative_source_size"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_bookmark_import_snapshots_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bookmark_import_snapshots")),
        sa.UniqueConstraint("user_id", "id", name="bookmark_import_snapshot_account_identity"),
        sa.UniqueConstraint(
            "user_id",
            "request_idempotency_key_hash",
            name="bookmark_import_snapshot_request_key_per_user",
        ),
        sa.UniqueConstraint(
            "user_id", "storage_key", name="bookmark_import_snapshot_storage_key_per_user"
        ),
    )
    with op.batch_alter_table("bookmark_import_snapshots", schema=None) as batch_op:
        batch_op.create_index(
            "ix_bookmark_import_snapshots_user_hash_created_id",
            ["user_id", "source_sha256", "created_at", "id"],
            unique=False,
        )

    op.create_table(
        "bookmark_import_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("parser_version", sa.String(length=64), nullable=False),
        sa.Column("normalizer_version", sa.String(length=64), nullable=False),
        sa.Column("skill_version", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("preview_version", sa.Integer(), nullable=False),
        sa.Column("progress_completed", sa.Integer(), nullable=False),
        sa.Column("progress_total", sa.Integer(), nullable=False),
        sa.Column("classification_budget", sa.Integer(), nullable=False),
        sa.Column("classification_used", sa.Integer(), nullable=False),
        sa.Column("lease_token_hash", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ("
            "'receiving', 'queued_parse', 'parsing', 'parse_preview_ready', "
            "'queued_classification', 'classifying', 'final_preview_ready', "
            "'committing', 'completed', 'completed_with_errors', "
            "'cancel_requested', 'cancelled', 'failed', 'expired'"
            ")",
            name=op.f("ck_bookmark_import_jobs_valid_state"),
        ),
        sa.CheckConstraint(
            "classification_budget >= 0 AND classification_used >= 0 "
            "AND classification_used <= classification_budget",
            name=op.f("ck_bookmark_import_jobs_valid_classification_budget"),
        ),
        sa.CheckConstraint(
            "preview_version >= 0", name=op.f("ck_bookmark_import_jobs_nonnegative_preview_version")
        ),
        sa.CheckConstraint(
            "progress_completed >= 0 AND progress_total >= 0 "
            "AND (progress_total = 0 OR progress_completed <= progress_total)",
            name=op.f("ck_bookmark_import_jobs_valid_progress"),
        ),
        sa.CheckConstraint("version > 0", name=op.f("ck_bookmark_import_jobs_positive_version")),
        sa.ForeignKeyConstraint(
            ["user_id", "snapshot_id"],
            ["bookmark_import_snapshots.user_id", "bookmark_import_snapshots.id"],
            name="bookmark_import_job_snapshot_same_account",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bookmark_import_jobs")),
        sa.UniqueConstraint("user_id", "id", name="bookmark_import_job_account_identity"),
        sa.UniqueConstraint("user_id", "snapshot_id", name="bookmark_import_job_snapshot_per_user"),
    )
    with op.batch_alter_table("bookmark_import_jobs", schema=None) as batch_op:
        batch_op.create_index(
            "ix_bookmark_import_jobs_lease_expiry",
            ["state", "lease_expires_at", "id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_bookmark_import_jobs_user_created_id", ["user_id", "created_at", "id"], unique=False
        )
        batch_op.create_index(
            "ix_bookmark_import_jobs_user_state_updated_id",
            ["user_id", "state", "updated_at", "id"],
            unique=False,
        )

    op.create_table(
        "bookmark_source_folders",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("source_folder_key", sa.String(length=128), nullable=False),
        sa.Column("parent_id", sa.String(length=36), nullable=True),
        sa.Column("source_sequence", sa.Integer(), nullable=False),
        sa.Column("source_order", sa.Integer(), nullable=False),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("display_path", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "depth BETWEEN 1 AND 64", name=op.f("ck_bookmark_source_folders_valid_depth")
        ),
        sa.CheckConstraint(
            "length(source_folder_key) BETWEEN 1 AND 128",
            name=op.f("ck_bookmark_source_folders_valid_source_key"),
        ),
        sa.CheckConstraint(
            "length(title) <= 256", name=op.f("ck_bookmark_source_folders_valid_title_length")
        ),
        sa.CheckConstraint(
            "source_sequence > 0 AND source_order > 0",
            name=op.f("ck_bookmark_source_folders_positive_order"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "snapshot_id", "parent_id"],
            [
                "bookmark_source_folders.user_id",
                "bookmark_source_folders.snapshot_id",
                "bookmark_source_folders.id",
            ],
            name="bookmark_source_folder_parent_same_snapshot",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "snapshot_id"],
            ["bookmark_import_snapshots.user_id", "bookmark_import_snapshots.id"],
            name="bookmark_source_folder_snapshot_same_account",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bookmark_source_folders")),
        sa.UniqueConstraint(
            "user_id", "snapshot_id", "id", name="bookmark_source_folder_snapshot_identity"
        ),
        sa.UniqueConstraint(
            "user_id",
            "snapshot_id",
            "source_folder_key",
            name="bookmark_source_folder_source_key_per_snapshot",
        ),
        sa.UniqueConstraint(
            "user_id",
            "snapshot_id",
            "source_sequence",
            name="bookmark_source_folder_sequence_per_snapshot",
        ),
    )
    with op.batch_alter_table("bookmark_source_folders", schema=None) as batch_op:
        batch_op.create_index(
            "ix_bookmark_source_folders_user_snapshot_parent_order_id",
            ["user_id", "snapshot_id", "parent_id", "source_order", "id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_bookmark_source_folders_user_snapshot_sequence_id",
            ["user_id", "snapshot_id", "source_sequence", "id"],
            unique=False,
        )

    op.create_table(
        "bookmark_import_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("run_idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("completion_hash", sa.String(length=64), nullable=True),
        sa.Column("parser_version", sa.String(length=64), nullable=False),
        sa.Column("normalizer_version", sa.String(length=64), nullable=False),
        sa.Column("source_sequence_count", sa.Integer(), nullable=False),
        sa.Column("folder_count", sa.Integer(), nullable=False),
        sa.Column("occurrence_count", sa.Integer(), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(state IN ('running', 'finalizing') AND completed_at IS NULL) OR "
            "(state IN ('complete', 'failed', 'cancelled') "
            "AND completed_at IS NOT NULL)",
            name=op.f("ck_bookmark_import_runs_terminal_completion_time"),
        ),
        sa.CheckConstraint(
            "state IN ('running', 'finalizing', 'complete', 'failed', 'cancelled')",
            name=op.f("ck_bookmark_import_runs_valid_state"),
        ),
        sa.CheckConstraint(
            "completion_hash IS NULL OR length(completion_hash) = 64",
            name=op.f("ck_bookmark_import_runs_valid_completion_hash"),
        ),
        sa.CheckConstraint(
            "state NOT IN ('finalizing', 'complete') OR completion_hash IS NOT NULL",
            name=op.f("ck_bookmark_import_runs_completion_hash_required"),
        ),
        sa.CheckConstraint(
            "attempt_number > 0", name=op.f("ck_bookmark_import_runs_positive_attempt_number")
        ),
        sa.CheckConstraint(
            "length(input_hash) = 64", name=op.f("ck_bookmark_import_runs_valid_input_hash")
        ),
        sa.CheckConstraint(
            "length(run_idempotency_key_hash) = 64",
            name=op.f("ck_bookmark_import_runs_valid_idempotency_key_hash"),
        ),
        sa.CheckConstraint(
            "source_sequence_count >= 0 AND folder_count >= 0 "
            "AND occurrence_count >= 0 AND candidate_count >= 0",
            name=op.f("ck_bookmark_import_runs_nonnegative_counts"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "job_id"],
            ["bookmark_import_jobs.user_id", "bookmark_import_jobs.id"],
            name="bookmark_import_run_job_same_account",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bookmark_import_runs")),
        sa.UniqueConstraint("user_id", "id", name="bookmark_import_run_account_identity"),
        sa.UniqueConstraint(
            "user_id", "job_id", "attempt_number", name="bookmark_import_run_attempt_per_job"
        ),
        sa.UniqueConstraint("user_id", "job_id", "id", name="bookmark_import_run_job_identity"),
        sa.UniqueConstraint(
            "user_id",
            "job_id",
            "run_idempotency_key_hash",
            name="bookmark_import_run_request_key_per_job",
        ),
    )
    with op.batch_alter_table("bookmark_import_runs", schema=None) as batch_op:
        batch_op.create_index(
            "ix_bookmark_import_runs_user_job_created_id",
            ["user_id", "job_id", "created_at", "id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_bookmark_import_runs_user_state_created_id",
            ["user_id", "state", "created_at", "id"],
            unique=False,
        )

    op.create_table(
        "bookmark_source_occurrences",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("source_occurrence_key", sa.String(length=128), nullable=False),
        sa.Column("folder_id", sa.String(length=36), nullable=True),
        sa.Column("source_sequence", sa.Integer(), nullable=False),
        sa.Column("source_order", sa.Integer(), nullable=False),
        sa.Column("raw_title", sa.String(length=1024), nullable=False),
        sa.Column("raw_url", sa.Text(), nullable=False),
        sa.Column("add_date", sa.Integer(), nullable=True),
        sa.Column("last_modified", sa.Integer(), nullable=True),
        sa.Column("validation_status", sa.String(length=16), nullable=False),
        sa.Column("fetch_policy", sa.String(length=40), nullable=True),
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        sa.Column("has_sensitive_url", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "fetch_policy IS NULL OR fetch_policy IN ("
            "'public_revalidation_required', 'export_metadata_only'"
            ")",
            name=op.f("ck_bookmark_source_occurrences_valid_fetch_policy"),
        ),
        sa.CheckConstraint(
            "validation_status IN ('accepted', 'invalid', 'unsupported')",
            name=op.f("ck_bookmark_source_occurrences_valid_validation_status"),
        ),
        sa.CheckConstraint(
            "add_date IS NULL OR add_date >= 0",
            name=op.f("ck_bookmark_source_occurrences_valid_add_date"),
        ),
        sa.CheckConstraint(
            "last_modified IS NULL OR last_modified >= 0",
            name=op.f("ck_bookmark_source_occurrences_valid_last_modified"),
        ),
        sa.CheckConstraint(
            "length(raw_title) <= 1024",
            name=op.f("ck_bookmark_source_occurrences_valid_title_length"),
        ),
        sa.CheckConstraint(
            "length(raw_url) <= 16384", name=op.f("ck_bookmark_source_occurrences_valid_url_length")
        ),
        sa.CheckConstraint(
            "length(source_occurrence_key) BETWEEN 1 AND 128",
            name=op.f("ck_bookmark_source_occurrences_valid_source_key"),
        ),
        sa.CheckConstraint(
            "source_sequence > 0 AND source_order > 0",
            name=op.f("ck_bookmark_source_occurrences_positive_order"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "snapshot_id", "folder_id"],
            [
                "bookmark_source_folders.user_id",
                "bookmark_source_folders.snapshot_id",
                "bookmark_source_folders.id",
            ],
            name="bookmark_source_occurrence_folder_same_snapshot",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "snapshot_id"],
            ["bookmark_import_snapshots.user_id", "bookmark_import_snapshots.id"],
            name="bookmark_source_occurrence_snapshot_same_account",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bookmark_source_occurrences")),
        sa.UniqueConstraint("user_id", "id", name="bookmark_source_occurrence_account_identity"),
        sa.UniqueConstraint(
            "user_id", "snapshot_id", "id", name="bookmark_source_occurrence_snapshot_identity"
        ),
        sa.UniqueConstraint(
            "user_id",
            "snapshot_id",
            "source_occurrence_key",
            name="bookmark_source_occurrence_source_key_per_snapshot",
        ),
        sa.UniqueConstraint(
            "user_id",
            "snapshot_id",
            "source_sequence",
            name="bookmark_source_occurrence_sequence_per_snapshot",
        ),
    )
    with op.batch_alter_table("bookmark_source_occurrences", schema=None) as batch_op:
        batch_op.create_index(
            "ix_bookmark_source_occurrences_user_snapshot_folder_order_id",
            ["user_id", "snapshot_id", "folder_id", "source_order", "id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_bookmark_source_occurrences_user_snapshot_sequence_id",
            ["user_id", "snapshot_id", "source_sequence", "id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_bookmark_source_occurrences_user_snapshot_status_sequence",
            ["user_id", "snapshot_id", "validation_status", "source_sequence"],
            unique=False,
        )

    op.create_table(
        "bookmark_import_checkpoints",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("phase", sa.String(length=24), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("source_sequence_start", sa.Integer(), nullable=True),
        sa.Column("source_sequence_end", sa.Integer(), nullable=True),
        sa.Column("processed_count", sa.Integer(), nullable=False),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "phase IN ('parse', 'classification', 'commit')",
            name=op.f("ck_bookmark_import_checkpoints_valid_phase"),
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'running', 'complete', 'failed', 'cancelled')",
            name=op.f("ck_bookmark_import_checkpoints_valid_state"),
        ),
        sa.CheckConstraint(
            "(source_sequence_start IS NULL AND source_sequence_end IS NULL) OR "
            "(source_sequence_start > 0 "
            "AND source_sequence_end >= source_sequence_start)",
            name=op.f("ck_bookmark_import_checkpoints_valid_source_sequence_range"),
        ),
        sa.CheckConstraint(
            "chunk_index >= 0", name=op.f("ck_bookmark_import_checkpoints_nonnegative_chunk_index")
        ),
        sa.CheckConstraint(
            "length(idempotency_key_hash) = 64",
            name=op.f("ck_bookmark_import_checkpoints_valid_idempotency_key_hash"),
        ),
        sa.CheckConstraint(
            "length(input_hash) = 64", name=op.f("ck_bookmark_import_checkpoints_valid_input_hash")
        ),
        sa.CheckConstraint(
            "processed_count >= 0",
            name=op.f("ck_bookmark_import_checkpoints_nonnegative_processed_count"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "run_id"],
            ["bookmark_import_runs.user_id", "bookmark_import_runs.id"],
            name="bookmark_import_checkpoint_run_same_account",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bookmark_import_checkpoints")),
        sa.UniqueConstraint("user_id", "id", name="bookmark_import_checkpoint_account_identity"),
        sa.UniqueConstraint(
            "user_id",
            "run_id",
            "phase",
            "chunk_index",
            name="bookmark_import_checkpoint_chunk_per_run",
        ),
        sa.UniqueConstraint(
            "user_id",
            "run_id",
            "phase",
            "idempotency_key_hash",
            name="bookmark_import_checkpoint_key_per_run",
        ),
    )
    with op.batch_alter_table("bookmark_import_checkpoints", schema=None) as batch_op:
        batch_op.create_index(
            "ix_bookmark_import_checkpoints_user_run_phase_state_chunk",
            ["user_id", "run_id", "phase", "state", "chunk_index"],
            unique=False,
        )

    op.create_table(
        "bookmark_import_current_runs",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("switched_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id", "job_id", "run_id"],
            [
                "bookmark_import_runs.user_id",
                "bookmark_import_runs.job_id",
                "bookmark_import_runs.id",
            ],
            name="bookmark_import_current_run_same_job",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "job_id"],
            ["bookmark_import_jobs.user_id", "bookmark_import_jobs.id"],
            name="bookmark_import_current_run_job_same_account",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "job_id", name=op.f("pk_bookmark_import_current_runs")),
        sa.UniqueConstraint("user_id", "run_id", name="bookmark_import_current_run_once"),
    )
    op.execute(
        """
        CREATE TRIGGER bookmark_import_runs_terminal_immutable
        BEFORE UPDATE ON bookmark_import_runs
        WHEN OLD.state IN ('complete', 'failed', 'cancelled')
        BEGIN
            SELECT RAISE(ABORT, 'terminal bookmark import runs are immutable');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER bookmark_import_current_run_insert_complete
        BEFORE INSERT ON bookmark_import_current_runs
        WHEN NOT EXISTS (
            SELECT 1
            FROM bookmark_import_runs AS run
            WHERE run.user_id = NEW.user_id
              AND run.job_id = NEW.job_id
              AND run.id = NEW.run_id
              AND run.state = 'complete'
        )
        BEGIN
            SELECT RAISE(ABORT, 'current bookmark import run must be complete');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER bookmark_import_current_run_update_complete
        BEFORE UPDATE ON bookmark_import_current_runs
        WHEN NOT EXISTS (
            SELECT 1
            FROM bookmark_import_runs AS run
            WHERE run.user_id = NEW.user_id
              AND run.job_id = NEW.job_id
              AND run.id = NEW.run_id
              AND run.state = 'complete'
        )
        BEGIN
            SELECT RAISE(ABORT, 'current bookmark import run must be complete');
        END
        """
    )
    op.create_table(
        "bookmark_staging_candidates",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("identity_url", sa.Text(), nullable=False),
        sa.Column("identity_hash", sa.String(length=64), nullable=False),
        sa.Column("display_title", sa.String(length=1024), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("fetch_policy", sa.String(length=40), nullable=False),
        sa.Column("has_sensitive_url", sa.Boolean(), nullable=False),
        sa.Column("proposed_action", sa.String(length=32), nullable=False),
        sa.Column("occurrence_count", sa.Integer(), nullable=False),
        sa.Column("first_source_sequence", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "fetch_policy IN ('public_revalidation_required', 'export_metadata_only')",
            name=op.f("ck_bookmark_staging_candidates_valid_fetch_policy"),
        ),
        sa.CheckConstraint(
            "proposed_action IN ("
            "'create', 'skip_existing', 'merge_missing_metadata', "
            "'reject', 'needs_review'"
            ")",
            name=op.f("ck_bookmark_staging_candidates_valid_proposed_action"),
        ),
        sa.CheckConstraint(
            "first_source_sequence > 0",
            name=op.f("ck_bookmark_staging_candidates_positive_first_sequence"),
        ),
        sa.CheckConstraint(
            "length(display_title) <= 1024",
            name=op.f("ck_bookmark_staging_candidates_valid_display_title_length"),
        ),
        sa.CheckConstraint(
            "length(host) BETWEEN 1 AND 255",
            name=op.f("ck_bookmark_staging_candidates_valid_host_length"),
        ),
        sa.CheckConstraint(
            "length(identity_hash) = 64",
            name=op.f("ck_bookmark_staging_candidates_valid_identity_hash"),
        ),
        sa.CheckConstraint(
            "length(identity_url) BETWEEN 1 AND 16384",
            name=op.f("ck_bookmark_staging_candidates_valid_identity_url"),
        ),
        sa.CheckConstraint(
            "occurrence_count > 0",
            name=op.f("ck_bookmark_staging_candidates_positive_occurrence_count"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "run_id"],
            ["bookmark_import_runs.user_id", "bookmark_import_runs.id"],
            name="bookmark_staging_candidate_run_same_account",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bookmark_staging_candidates")),
        sa.UniqueConstraint(
            "user_id", "run_id", "id", name="bookmark_staging_candidate_run_identity"
        ),
        sa.UniqueConstraint(
            "user_id",
            "run_id",
            "identity_hash",
            "identity_url",
            name="bookmark_staging_candidate_identity_per_run",
        ),
    )
    with op.batch_alter_table("bookmark_staging_candidates", schema=None) as batch_op:
        batch_op.create_index(
            "ix_bookmark_staging_candidates_user_run_action_sequence",
            ["user_id", "run_id", "proposed_action", "first_source_sequence"],
            unique=False,
        )
        batch_op.create_index(
            "ix_bookmark_staging_candidates_user_run_hash_id",
            ["user_id", "run_id", "identity_hash", "id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_bookmark_staging_candidates_user_run_sequence_id",
            ["user_id", "run_id", "first_source_sequence", "id"],
            unique=False,
        )

    op.create_table(
        "bookmark_staging_folders",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("source_folder_key", sa.String(length=128), nullable=False),
        sa.Column("parent_id", sa.String(length=36), nullable=True),
        sa.Column("source_sequence", sa.Integer(), nullable=False),
        sa.Column("source_order", sa.Integer(), nullable=False),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("display_path", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "depth BETWEEN 1 AND 64", name=op.f("ck_bookmark_staging_folders_valid_depth")
        ),
        sa.CheckConstraint(
            "length(source_folder_key) BETWEEN 1 AND 128",
            name=op.f("ck_bookmark_staging_folders_valid_source_key"),
        ),
        sa.CheckConstraint(
            "length(title) <= 256", name=op.f("ck_bookmark_staging_folders_valid_title_length")
        ),
        sa.CheckConstraint(
            "source_sequence > 0 AND source_order > 0",
            name=op.f("ck_bookmark_staging_folders_positive_order"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "run_id", "parent_id"],
            [
                "bookmark_staging_folders.user_id",
                "bookmark_staging_folders.run_id",
                "bookmark_staging_folders.id",
            ],
            name="bookmark_staging_folder_parent_same_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "run_id"],
            ["bookmark_import_runs.user_id", "bookmark_import_runs.id"],
            name="bookmark_staging_folder_run_same_account",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bookmark_staging_folders")),
        sa.UniqueConstraint("user_id", "run_id", "id", name="bookmark_staging_folder_run_identity"),
        sa.UniqueConstraint(
            "user_id",
            "run_id",
            "source_folder_key",
            name="bookmark_staging_folder_source_key_per_run",
        ),
        sa.UniqueConstraint(
            "user_id", "run_id", "source_sequence", name="bookmark_staging_folder_sequence_per_run"
        ),
    )
    with op.batch_alter_table("bookmark_staging_folders", schema=None) as batch_op:
        batch_op.create_index(
            "ix_bookmark_staging_folders_user_run_parent_order_id",
            ["user_id", "run_id", "parent_id", "source_order", "id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_bookmark_staging_folders_user_run_sequence_id",
            ["user_id", "run_id", "source_sequence", "id"],
            unique=False,
        )

    op.create_table(
        "site_import_origins",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("site_id", sa.String(length=36), nullable=False),
        sa.Column("source_occurrence_id", sa.String(length=36), nullable=False),
        sa.Column("link_action", sa.String(length=24), nullable=False),
        sa.Column("site_version_at_link", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "link_action IN ('created_site', 'matched_existing')",
            name=op.f("ck_site_import_origins_valid_link_action"),
        ),
        sa.CheckConstraint(
            "site_version_at_link > 0", name=op.f("ck_site_import_origins_positive_site_version")
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "site_id"],
            ["sites.user_id", "sites.id"],
            name="site_import_origin_site_same_account",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "source_occurrence_id"],
            ["bookmark_source_occurrences.user_id", "bookmark_source_occurrences.id"],
            name="site_import_origin_occurrence_same_account",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_site_import_origins")),
        sa.UniqueConstraint("user_id", "id", name="site_import_origin_account_identity"),
        sa.UniqueConstraint(
            "user_id", "source_occurrence_id", name="site_import_origin_occurrence_once"
        ),
    )
    with op.batch_alter_table("site_import_origins", schema=None) as batch_op:
        batch_op.create_index(
            "ix_site_import_origins_user_site_created_id",
            ["user_id", "site_id", "created_at", "id"],
            unique=False,
        )

    op.create_table(
        "bookmark_staging_candidate_folders",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("candidate_id", sa.String(length=36), nullable=False),
        sa.Column("folder_scope_key", sa.String(length=128), nullable=False),
        sa.Column("folder_id", sa.String(length=36), nullable=True),
        sa.Column("occurrence_count", sa.Integer(), nullable=False),
        sa.Column("first_source_sequence", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "first_source_sequence > 0",
            name=op.f("ck_bookmark_staging_candidate_folders_positive_first_sequence"),
        ),
        sa.CheckConstraint(
            "length(folder_scope_key) BETWEEN 1 AND 128",
            name=op.f("ck_bookmark_staging_candidate_folders_valid_scope_key"),
        ),
        sa.CheckConstraint(
            "occurrence_count > 0",
            name=op.f("ck_bookmark_staging_candidate_folders_positive_occurrence_count"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "run_id", "candidate_id"],
            [
                "bookmark_staging_candidates.user_id",
                "bookmark_staging_candidates.run_id",
                "bookmark_staging_candidates.id",
            ],
            name="bookmark_staging_candidate_folder_candidate_same_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "run_id", "folder_id"],
            [
                "bookmark_staging_folders.user_id",
                "bookmark_staging_folders.run_id",
                "bookmark_staging_folders.id",
            ],
            name="bookmark_staging_candidate_folder_folder_same_run",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "user_id",
            "run_id",
            "candidate_id",
            "folder_scope_key",
            name=op.f("pk_bookmark_staging_candidate_folders"),
        ),
    )
    with op.batch_alter_table("bookmark_staging_candidate_folders", schema=None) as batch_op:
        batch_op.create_index(
            "ix_bookmark_staging_candidate_folders_user_run_folder_candidate",
            ["user_id", "run_id", "folder_id", "candidate_id"],
            unique=False,
        )

    op.create_table(
        "bookmark_staging_candidate_site_matches",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("candidate_id", sa.String(length=36), nullable=False),
        sa.Column("site_id", sa.String(length=36), nullable=False),
        sa.Column("site_version", sa.Integer(), nullable=False),
        sa.Column("matched_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "site_version > 0",
            name=op.f("ck_bookmark_staging_candidate_site_matches_positive_site_version"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "run_id", "candidate_id"],
            [
                "bookmark_staging_candidates.user_id",
                "bookmark_staging_candidates.run_id",
                "bookmark_staging_candidates.id",
            ],
            name="bookmark_staging_candidate_site_match_candidate_same_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "site_id"],
            ["sites.user_id", "sites.id"],
            name="bookmark_staging_candidate_site_match_site_same_account",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "user_id",
            "run_id",
            "candidate_id",
            name=op.f("pk_bookmark_staging_candidate_site_matches"),
        ),
        sa.UniqueConstraint(
            "user_id", "run_id", "site_id", name="bookmark_staging_candidate_site_once_per_run"
        ),
    )
    op.create_table(
        "bookmark_staging_occurrences",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("source_occurrence_key", sa.String(length=128), nullable=False),
        sa.Column("folder_id", sa.String(length=36), nullable=True),
        sa.Column("source_sequence", sa.Integer(), nullable=False),
        sa.Column("source_order", sa.Integer(), nullable=False),
        sa.Column("raw_title", sa.String(length=1024), nullable=False),
        sa.Column("raw_url", sa.Text(), nullable=False),
        sa.Column("add_date", sa.Integer(), nullable=True),
        sa.Column("last_modified", sa.Integer(), nullable=True),
        sa.Column("validation_status", sa.String(length=16), nullable=False),
        sa.Column("fetch_policy", sa.String(length=40), nullable=True),
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        sa.Column("has_sensitive_url", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "fetch_policy IS NULL OR fetch_policy IN ("
            "'public_revalidation_required', 'export_metadata_only'"
            ")",
            name=op.f("ck_bookmark_staging_occurrences_valid_fetch_policy"),
        ),
        sa.CheckConstraint(
            "validation_status IN ('accepted', 'invalid', 'unsupported')",
            name=op.f("ck_bookmark_staging_occurrences_valid_validation_status"),
        ),
        sa.CheckConstraint(
            "add_date IS NULL OR add_date >= 0",
            name=op.f("ck_bookmark_staging_occurrences_valid_add_date"),
        ),
        sa.CheckConstraint(
            "last_modified IS NULL OR last_modified >= 0",
            name=op.f("ck_bookmark_staging_occurrences_valid_last_modified"),
        ),
        sa.CheckConstraint(
            "length(raw_title) <= 1024",
            name=op.f("ck_bookmark_staging_occurrences_valid_title_length"),
        ),
        sa.CheckConstraint(
            "length(raw_url) <= 16384",
            name=op.f("ck_bookmark_staging_occurrences_valid_url_length"),
        ),
        sa.CheckConstraint(
            "length(source_occurrence_key) BETWEEN 1 AND 128",
            name=op.f("ck_bookmark_staging_occurrences_valid_source_key"),
        ),
        sa.CheckConstraint(
            "source_sequence > 0 AND source_order > 0",
            name=op.f("ck_bookmark_staging_occurrences_positive_order"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "run_id", "folder_id"],
            [
                "bookmark_staging_folders.user_id",
                "bookmark_staging_folders.run_id",
                "bookmark_staging_folders.id",
            ],
            name="bookmark_staging_occurrence_folder_same_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "run_id"],
            ["bookmark_import_runs.user_id", "bookmark_import_runs.id"],
            name="bookmark_staging_occurrence_run_same_account",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bookmark_staging_occurrences")),
        sa.UniqueConstraint(
            "user_id", "run_id", "id", name="bookmark_staging_occurrence_run_identity"
        ),
        sa.UniqueConstraint(
            "user_id",
            "run_id",
            "source_occurrence_key",
            name="bookmark_staging_occurrence_source_key_per_run",
        ),
        sa.UniqueConstraint(
            "user_id",
            "run_id",
            "source_sequence",
            name="bookmark_staging_occurrence_sequence_per_run",
        ),
    )
    with op.batch_alter_table("bookmark_staging_occurrences", schema=None) as batch_op:
        batch_op.create_index(
            "ix_bookmark_staging_occurrences_user_run_folder_order_id",
            ["user_id", "run_id", "folder_id", "source_order", "id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_bookmark_staging_occurrences_user_run_sequence_id",
            ["user_id", "run_id", "source_sequence", "id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_bookmark_staging_occurrences_user_run_status_sequence",
            ["user_id", "run_id", "validation_status", "source_sequence"],
            unique=False,
        )

    op.create_table(
        "bookmark_staging_candidate_occurrences",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("candidate_id", sa.String(length=36), nullable=False),
        sa.Column("occurrence_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id", "run_id", "candidate_id"],
            [
                "bookmark_staging_candidates.user_id",
                "bookmark_staging_candidates.run_id",
                "bookmark_staging_candidates.id",
            ],
            name="bookmark_staging_candidate_occurrence_candidate_same_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "run_id", "occurrence_id"],
            [
                "bookmark_staging_occurrences.user_id",
                "bookmark_staging_occurrences.run_id",
                "bookmark_staging_occurrences.id",
            ],
            name="bookmark_staging_candidate_occurrence_occurrence_same_run",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "user_id",
            "run_id",
            "candidate_id",
            "occurrence_id",
            name=op.f("pk_bookmark_staging_candidate_occurrences"),
        ),
        sa.UniqueConstraint(
            "user_id", "run_id", "occurrence_id", name="bookmark_staging_occurrence_candidate_once"
        ),
    )
    with op.batch_alter_table("bookmark_staging_candidate_occurrences", schema=None) as batch_op:
        batch_op.create_index(
            "ix_bookmark_staging_candidate_occurrences_user_run_candidate_occurrence",
            ["user_id", "run_id", "candidate_id", "occurrence_id"],
            unique=False,
        )

    _create_terminal_run_child_triggers()

    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    _drop_terminal_run_child_triggers()
    op.execute("DROP TRIGGER IF EXISTS bookmark_import_current_run_update_complete")
    op.execute("DROP TRIGGER IF EXISTS bookmark_import_current_run_insert_complete")
    op.execute("DROP TRIGGER IF EXISTS bookmark_import_runs_terminal_immutable")
    with op.batch_alter_table("bookmark_staging_candidate_occurrences", schema=None) as batch_op:
        batch_op.drop_index(
            "ix_bookmark_staging_candidate_occurrences_user_run_candidate_occurrence"
        )

    op.drop_table("bookmark_staging_candidate_occurrences")
    with op.batch_alter_table("bookmark_staging_occurrences", schema=None) as batch_op:
        batch_op.drop_index("ix_bookmark_staging_occurrences_user_run_status_sequence")
        batch_op.drop_index("ix_bookmark_staging_occurrences_user_run_sequence_id")
        batch_op.drop_index("ix_bookmark_staging_occurrences_user_run_folder_order_id")

    op.drop_table("bookmark_staging_occurrences")
    op.drop_table("bookmark_staging_candidate_site_matches")
    with op.batch_alter_table("bookmark_staging_candidate_folders", schema=None) as batch_op:
        batch_op.drop_index("ix_bookmark_staging_candidate_folders_user_run_folder_candidate")

    op.drop_table("bookmark_staging_candidate_folders")
    with op.batch_alter_table("site_import_origins", schema=None) as batch_op:
        batch_op.drop_index("ix_site_import_origins_user_site_created_id")

    op.drop_table("site_import_origins")
    with op.batch_alter_table("bookmark_staging_folders", schema=None) as batch_op:
        batch_op.drop_index("ix_bookmark_staging_folders_user_run_sequence_id")
        batch_op.drop_index("ix_bookmark_staging_folders_user_run_parent_order_id")

    op.drop_table("bookmark_staging_folders")
    with op.batch_alter_table("bookmark_staging_candidates", schema=None) as batch_op:
        batch_op.drop_index("ix_bookmark_staging_candidates_user_run_sequence_id")
        batch_op.drop_index("ix_bookmark_staging_candidates_user_run_hash_id")
        batch_op.drop_index("ix_bookmark_staging_candidates_user_run_action_sequence")

    op.drop_table("bookmark_staging_candidates")
    op.drop_table("bookmark_import_current_runs")
    with op.batch_alter_table("bookmark_import_checkpoints", schema=None) as batch_op:
        batch_op.drop_index("ix_bookmark_import_checkpoints_user_run_phase_state_chunk")

    op.drop_table("bookmark_import_checkpoints")
    with op.batch_alter_table("bookmark_source_occurrences", schema=None) as batch_op:
        batch_op.drop_index("ix_bookmark_source_occurrences_user_snapshot_status_sequence")
        batch_op.drop_index("ix_bookmark_source_occurrences_user_snapshot_sequence_id")
        batch_op.drop_index("ix_bookmark_source_occurrences_user_snapshot_folder_order_id")

    op.drop_table("bookmark_source_occurrences")
    with op.batch_alter_table("bookmark_import_runs", schema=None) as batch_op:
        batch_op.drop_index("ix_bookmark_import_runs_user_state_created_id")
        batch_op.drop_index("ix_bookmark_import_runs_user_job_created_id")

    op.drop_table("bookmark_import_runs")
    with op.batch_alter_table("bookmark_source_folders", schema=None) as batch_op:
        batch_op.drop_index("ix_bookmark_source_folders_user_snapshot_sequence_id")
        batch_op.drop_index("ix_bookmark_source_folders_user_snapshot_parent_order_id")

    op.drop_table("bookmark_source_folders")
    with op.batch_alter_table("bookmark_import_jobs", schema=None) as batch_op:
        batch_op.drop_index("ix_bookmark_import_jobs_user_state_updated_id")
        batch_op.drop_index("ix_bookmark_import_jobs_user_created_id")
        batch_op.drop_index("ix_bookmark_import_jobs_lease_expiry")

    op.drop_table("bookmark_import_jobs")
    with op.batch_alter_table("bookmark_import_snapshots", schema=None) as batch_op:
        batch_op.drop_index("ix_bookmark_import_snapshots_user_hash_created_id")

    op.drop_table("bookmark_import_snapshots")
    # ### end Alembic commands ###
