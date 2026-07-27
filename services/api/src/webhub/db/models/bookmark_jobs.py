"""书签导入任务的生命周期：快照、任务、运行、断点。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from webhub.db.models._base import Base, new_id, utc_now


class BookmarkImportSnapshot(Base):
    __tablename__ = "bookmark_import_snapshots"
    __table_args__ = (
        UniqueConstraint("user_id", "id", name="bookmark_import_snapshot_account_identity"),
        UniqueConstraint(
            "user_id",
            "request_idempotency_key_hash",
            name="bookmark_import_snapshot_request_key_per_user",
        ),
        UniqueConstraint(
            "user_id", "storage_key", name="bookmark_import_snapshot_storage_key_per_user"
        ),
        CheckConstraint("length(source_sha256) = 64", name="valid_source_sha256"),
        CheckConstraint(
            "length(request_idempotency_key_hash) = 64",
            name="valid_request_idempotency_key_hash",
        ),
        CheckConstraint("source_size_bytes >= 0", name="nonnegative_source_size"),
        CheckConstraint("source_format = 'netscape_html'", name="valid_source_format"),
        Index(
            "ix_bookmark_import_snapshots_user_hash_created_id",
            "user_id",
            "source_sha256",
            "created_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    source_format: Mapped[str] = mapped_column(String(32), nullable=False)
    original_filename: Mapped[str | None] = mapped_column(String(255))
    storage_key: Mapped[str] = mapped_column(String(255), nullable=False)
    detected_encoding: Mapped[str | None] = mapped_column(String(40))
    request_idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class BookmarkImportJob(Base):
    __tablename__ = "bookmark_import_jobs"
    __table_args__ = (
        UniqueConstraint("user_id", "id", name="bookmark_import_job_account_identity"),
        UniqueConstraint("user_id", "snapshot_id", name="bookmark_import_job_snapshot_per_user"),
        ForeignKeyConstraint(
            ["user_id", "snapshot_id"],
            ["bookmark_import_snapshots.user_id", "bookmark_import_snapshots.id"],
            name="bookmark_import_job_snapshot_same_account",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "state IN ("
            "'receiving', 'queued_parse', 'parsing', 'parse_preview_ready', "
            "'queued_classification', 'classifying', 'final_preview_ready', "
            "'committing', 'completed', 'completed_with_errors', "
            "'cancel_requested', 'cancelled', 'failed', 'expired'"
            ")",
            name="valid_state",
        ),
        CheckConstraint("version > 0", name="positive_version"),
        CheckConstraint("preview_version >= 0", name="nonnegative_preview_version"),
        CheckConstraint(
            "progress_completed >= 0 AND progress_total >= 0 "
            "AND (progress_total = 0 OR progress_completed <= progress_total)",
            name="valid_progress",
        ),
        CheckConstraint(
            "classification_budget >= 0 AND classification_used >= 0 "
            "AND classification_used <= classification_budget",
            name="valid_classification_budget",
        ),
        Index(
            "ix_bookmark_import_jobs_user_state_updated_id", "user_id", "state", "updated_at", "id"
        ),
        Index("ix_bookmark_import_jobs_user_created_id", "user_id", "created_at", "id"),
        Index("ix_bookmark_import_jobs_lease_expiry", "state", "lease_expires_at", "id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    snapshot_id: Mapped[str] = mapped_column(String(36), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="receiving")
    parser_version: Mapped[str] = mapped_column(String(64), nullable=False)
    normalizer_version: Mapped[str] = mapped_column(String(64), nullable=False)
    skill_version: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    preview_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    progress_completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    classification_budget: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    classification_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_token_hash: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BookmarkImportRun(Base):
    __tablename__ = "bookmark_import_runs"
    __table_args__ = (
        UniqueConstraint("user_id", "id", name="bookmark_import_run_account_identity"),
        UniqueConstraint("user_id", "job_id", "id", name="bookmark_import_run_job_identity"),
        UniqueConstraint(
            "user_id", "job_id", "attempt_number", name="bookmark_import_run_attempt_per_job"
        ),
        UniqueConstraint(
            "user_id",
            "job_id",
            "run_idempotency_key_hash",
            name="bookmark_import_run_request_key_per_job",
        ),
        ForeignKeyConstraint(
            ["user_id", "job_id"],
            ["bookmark_import_jobs.user_id", "bookmark_import_jobs.id"],
            name="bookmark_import_run_job_same_account",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "state IN ('running', 'finalizing', 'complete', 'failed', 'cancelled')",
            name="valid_state",
        ),
        CheckConstraint("attempt_number > 0", name="positive_attempt_number"),
        CheckConstraint("length(run_idempotency_key_hash) = 64", name="valid_idempotency_key_hash"),
        CheckConstraint("length(input_hash) = 64", name="valid_input_hash"),
        CheckConstraint(
            "completion_hash IS NULL OR length(completion_hash) = 64",
            name="valid_completion_hash",
        ),
        CheckConstraint(
            "state NOT IN ('finalizing', 'complete') OR completion_hash IS NOT NULL",
            name="completion_hash_required",
        ),
        CheckConstraint(
            "source_sequence_count >= 0 AND folder_count >= 0 "
            "AND occurrence_count >= 0 AND candidate_count >= 0",
            name="nonnegative_counts",
        ),
        CheckConstraint(
            "(state IN ('running', 'finalizing') AND completed_at IS NULL) OR "
            "(state IN ('complete', 'failed', 'cancelled') AND completed_at IS NOT NULL)",
            name="terminal_completion_time",
        ),
        Index(
            "ix_bookmark_import_runs_user_job_created_id", "user_id", "job_id", "created_at", "id"
        ),
        Index(
            "ix_bookmark_import_runs_user_state_created_id", "user_id", "state", "created_at", "id"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    job_id: Mapped[str] = mapped_column(String(36), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    run_idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    completion_hash: Mapped[str | None] = mapped_column(String(64))
    parser_version: Mapped[str] = mapped_column(String(64), nullable=False)
    normalizer_version: Mapped[str] = mapped_column(String(64), nullable=False)
    source_sequence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    folder_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BookmarkImportCurrentRun(Base):
    __tablename__ = "bookmark_import_current_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "job_id"],
            ["bookmark_import_jobs.user_id", "bookmark_import_jobs.id"],
            name="bookmark_import_current_run_job_same_account",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["user_id", "job_id", "run_id"],
            [
                "bookmark_import_runs.user_id",
                "bookmark_import_runs.job_id",
                "bookmark_import_runs.id",
            ],
            name="bookmark_import_current_run_same_job",
            ondelete="CASCADE",
        ),
        UniqueConstraint("user_id", "run_id", name="bookmark_import_current_run_once"),
    )

    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    switched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class BookmarkImportCheckpoint(Base):
    __tablename__ = "bookmark_import_checkpoints"
    __table_args__ = (
        UniqueConstraint("user_id", "id", name="bookmark_import_checkpoint_account_identity"),
        UniqueConstraint(
            "user_id",
            "run_id",
            "phase",
            "chunk_index",
            name="bookmark_import_checkpoint_chunk_per_run",
        ),
        UniqueConstraint(
            "user_id",
            "run_id",
            "phase",
            "idempotency_key_hash",
            name="bookmark_import_checkpoint_key_per_run",
        ),
        ForeignKeyConstraint(
            ["user_id", "run_id"],
            ["bookmark_import_runs.user_id", "bookmark_import_runs.id"],
            name="bookmark_import_checkpoint_run_same_account",
            ondelete="CASCADE",
        ),
        CheckConstraint("phase IN ('parse', 'classification', 'commit')", name="valid_phase"),
        CheckConstraint(
            "state IN ('pending', 'running', 'complete', 'failed', 'cancelled')",
            name="valid_state",
        ),
        CheckConstraint("chunk_index >= 0", name="nonnegative_chunk_index"),
        CheckConstraint("length(idempotency_key_hash) = 64", name="valid_idempotency_key_hash"),
        CheckConstraint("length(input_hash) = 64", name="valid_input_hash"),
        CheckConstraint(
            "(source_sequence_start IS NULL AND source_sequence_end IS NULL) OR "
            "(source_sequence_start > 0 AND source_sequence_end >= source_sequence_start)",
            name="valid_source_sequence_range",
        ),
        CheckConstraint("processed_count >= 0", name="nonnegative_processed_count"),
        Index(
            "ix_bookmark_import_checkpoints_user_run_phase_state_chunk",
            "user_id",
            "run_id",
            "phase",
            "state",
            "chunk_index",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    phase: Mapped[str] = mapped_column(String(24), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    source_sequence_start: Mapped[int | None] = mapped_column(Integer)
    source_sequence_end: Mapped[int | None] = mapped_column(Integer)
    processed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
