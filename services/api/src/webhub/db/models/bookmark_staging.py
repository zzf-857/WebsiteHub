"""书签导入的暂存区：目录、出现、候选及其投影。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from webhub.db.models._base import Base, new_id, utc_now


class BookmarkStagingFolder(Base):
    __tablename__ = "bookmark_staging_folders"
    __table_args__ = (
        UniqueConstraint("user_id", "run_id", "id", name="bookmark_staging_folder_run_identity"),
        UniqueConstraint(
            "user_id",
            "run_id",
            "source_folder_key",
            name="bookmark_staging_folder_source_key_per_run",
        ),
        UniqueConstraint(
            "user_id",
            "run_id",
            "source_sequence",
            name="bookmark_staging_folder_sequence_per_run",
        ),
        ForeignKeyConstraint(
            ["user_id", "run_id"],
            ["bookmark_import_runs.user_id", "bookmark_import_runs.id"],
            name="bookmark_staging_folder_run_same_account",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["user_id", "run_id", "parent_id"],
            [
                "bookmark_staging_folders.user_id",
                "bookmark_staging_folders.run_id",
                "bookmark_staging_folders.id",
            ],
            name="bookmark_staging_folder_parent_same_run",
            ondelete="CASCADE",
        ),
        CheckConstraint("length(source_folder_key) BETWEEN 1 AND 128", name="valid_source_key"),
        CheckConstraint("source_sequence > 0 AND source_order > 0", name="positive_order"),
        CheckConstraint("depth BETWEEN 1 AND 64", name="valid_depth"),
        CheckConstraint("length(title) <= 256", name="valid_title_length"),
        Index(
            "ix_bookmark_staging_folders_user_run_sequence_id",
            "user_id",
            "run_id",
            "source_sequence",
            "id",
        ),
        Index(
            "ix_bookmark_staging_folders_user_run_parent_order_id",
            "user_id",
            "run_id",
            "parent_id",
            "source_order",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_folder_key: Mapped[str] = mapped_column(String(128), nullable=False)
    parent_id: Mapped[str | None] = mapped_column(String(36))
    source_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    source_order: Mapped[int] = mapped_column(Integer, nullable=False)
    depth: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    display_path: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class BookmarkStagingOccurrence(Base):
    __tablename__ = "bookmark_staging_occurrences"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "run_id", "id", name="bookmark_staging_occurrence_run_identity"
        ),
        UniqueConstraint(
            "user_id",
            "run_id",
            "source_occurrence_key",
            name="bookmark_staging_occurrence_source_key_per_run",
        ),
        UniqueConstraint(
            "user_id",
            "run_id",
            "source_sequence",
            name="bookmark_staging_occurrence_sequence_per_run",
        ),
        ForeignKeyConstraint(
            ["user_id", "run_id"],
            ["bookmark_import_runs.user_id", "bookmark_import_runs.id"],
            name="bookmark_staging_occurrence_run_same_account",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["user_id", "run_id", "folder_id"],
            [
                "bookmark_staging_folders.user_id",
                "bookmark_staging_folders.run_id",
                "bookmark_staging_folders.id",
            ],
            name="bookmark_staging_occurrence_folder_same_run",
            ondelete="CASCADE",
        ),
        CheckConstraint("length(source_occurrence_key) BETWEEN 1 AND 128", name="valid_source_key"),
        CheckConstraint("source_sequence > 0 AND source_order > 0", name="positive_order"),
        CheckConstraint("length(raw_title) <= 1024", name="valid_title_length"),
        CheckConstraint("length(raw_url) <= 16384", name="valid_url_length"),
        CheckConstraint(
            "validation_status IN ('accepted', 'invalid', 'unsupported')",
            name="valid_validation_status",
        ),
        CheckConstraint(
            "fetch_policy IS NULL OR fetch_policy IN ("
            "'public_revalidation_required', 'export_metadata_only'"
            ")",
            name="valid_fetch_policy",
        ),
        CheckConstraint("add_date IS NULL OR add_date >= 0", name="valid_add_date"),
        CheckConstraint("last_modified IS NULL OR last_modified >= 0", name="valid_last_modified"),
        Index(
            "ix_bookmark_staging_occurrences_user_run_sequence_id",
            "user_id",
            "run_id",
            "source_sequence",
            "id",
        ),
        Index(
            "ix_bookmark_staging_occurrences_user_run_status_sequence",
            "user_id",
            "run_id",
            "validation_status",
            "source_sequence",
        ),
        Index(
            "ix_bookmark_staging_occurrences_user_run_folder_order_id",
            "user_id",
            "run_id",
            "folder_id",
            "source_order",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_occurrence_key: Mapped[str] = mapped_column(String(128), nullable=False)
    folder_id: Mapped[str | None] = mapped_column(String(36))
    source_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    source_order: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_title: Mapped[str] = mapped_column(String(1024), nullable=False)
    raw_url: Mapped[str] = mapped_column(Text, nullable=False)
    add_date: Mapped[int | None] = mapped_column(Integer)
    last_modified: Mapped[int | None] = mapped_column(Integer)
    validation_status: Mapped[str] = mapped_column(String(16), nullable=False)
    fetch_policy: Mapped[str | None] = mapped_column(String(40))
    reason_code: Mapped[str | None] = mapped_column(String(64))
    has_sensitive_url: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class BookmarkStagingCandidate(Base):
    __tablename__ = "bookmark_staging_candidates"
    __table_args__ = (
        UniqueConstraint("user_id", "run_id", "id", name="bookmark_staging_candidate_run_identity"),
        UniqueConstraint(
            "user_id",
            "run_id",
            "identity_hash",
            "identity_url",
            name="bookmark_staging_candidate_identity_per_run",
        ),
        ForeignKeyConstraint(
            ["user_id", "run_id"],
            ["bookmark_import_runs.user_id", "bookmark_import_runs.id"],
            name="bookmark_staging_candidate_run_same_account",
            ondelete="CASCADE",
        ),
        CheckConstraint("length(identity_hash) = 64", name="valid_identity_hash"),
        CheckConstraint("length(identity_url) BETWEEN 1 AND 16384", name="valid_identity_url"),
        CheckConstraint("length(display_title) <= 1024", name="valid_display_title_length"),
        CheckConstraint("length(host) BETWEEN 1 AND 255", name="valid_host_length"),
        CheckConstraint(
            "fetch_policy IN ('public_revalidation_required', 'export_metadata_only')",
            name="valid_fetch_policy",
        ),
        CheckConstraint(
            "proposed_action IN ("
            "'create', 'skip_existing', 'merge_missing_metadata', 'reject', 'needs_review'"
            ")",
            name="valid_proposed_action",
        ),
        CheckConstraint("occurrence_count > 0", name="positive_occurrence_count"),
        CheckConstraint("first_source_sequence > 0", name="positive_first_sequence"),
        Index(
            "ix_bookmark_staging_candidates_user_run_sequence_id",
            "user_id",
            "run_id",
            "first_source_sequence",
            "id",
        ),
        Index(
            "ix_bookmark_staging_candidates_user_run_action_sequence",
            "user_id",
            "run_id",
            "proposed_action",
            "first_source_sequence",
        ),
        Index(
            "ix_bookmark_staging_candidates_user_run_hash_id",
            "user_id",
            "run_id",
            "identity_hash",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    identity_url: Mapped[str] = mapped_column(Text, nullable=False)
    identity_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    display_title: Mapped[str] = mapped_column(String(1024), nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    fetch_policy: Mapped[str] = mapped_column(String(40), nullable=False)
    has_sensitive_url: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    proposed_action: Mapped[str] = mapped_column(String(32), nullable=False)
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False)
    first_source_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class BookmarkStagingCandidateOccurrence(Base):
    __tablename__ = "bookmark_staging_candidate_occurrences"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "run_id", "candidate_id"],
            [
                "bookmark_staging_candidates.user_id",
                "bookmark_staging_candidates.run_id",
                "bookmark_staging_candidates.id",
            ],
            name="bookmark_staging_candidate_occurrence_candidate_same_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["user_id", "run_id", "occurrence_id"],
            [
                "bookmark_staging_occurrences.user_id",
                "bookmark_staging_occurrences.run_id",
                "bookmark_staging_occurrences.id",
            ],
            name="bookmark_staging_candidate_occurrence_occurrence_same_run",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "user_id",
            "run_id",
            "occurrence_id",
            name="bookmark_staging_occurrence_candidate_once",
        ),
        Index(
            "ix_bookmark_staging_candidate_occurrences_user_run_candidate_occurrence",
            "user_id",
            "run_id",
            "candidate_id",
            "occurrence_id",
        ),
    )

    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    occurrence_id: Mapped[str] = mapped_column(String(36), primary_key=True)


class BookmarkStagingCandidateFolder(Base):
    __tablename__ = "bookmark_staging_candidate_folders"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "run_id", "candidate_id"],
            [
                "bookmark_staging_candidates.user_id",
                "bookmark_staging_candidates.run_id",
                "bookmark_staging_candidates.id",
            ],
            name="bookmark_staging_candidate_folder_candidate_same_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["user_id", "run_id", "folder_id"],
            [
                "bookmark_staging_folders.user_id",
                "bookmark_staging_folders.run_id",
                "bookmark_staging_folders.id",
            ],
            name="bookmark_staging_candidate_folder_folder_same_run",
            ondelete="CASCADE",
        ),
        CheckConstraint("length(folder_scope_key) BETWEEN 1 AND 128", name="valid_scope_key"),
        CheckConstraint("occurrence_count > 0", name="positive_occurrence_count"),
        CheckConstraint("first_source_sequence > 0", name="positive_first_sequence"),
        Index(
            "ix_bookmark_staging_candidate_folders_user_run_folder_candidate",
            "user_id",
            "run_id",
            "folder_id",
            "candidate_id",
        ),
    )

    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    folder_scope_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    folder_id: Mapped[str | None] = mapped_column(String(36))
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False)
    first_source_sequence: Mapped[int] = mapped_column(Integer, nullable=False)


class BookmarkStagingCandidateSiteMatch(Base):
    __tablename__ = "bookmark_staging_candidate_site_matches"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "run_id", "candidate_id"],
            [
                "bookmark_staging_candidates.user_id",
                "bookmark_staging_candidates.run_id",
                "bookmark_staging_candidates.id",
            ],
            name="bookmark_staging_candidate_site_match_candidate_same_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["user_id", "site_id"],
            ["sites.user_id", "sites.id"],
            name="bookmark_staging_candidate_site_match_site_same_account",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "user_id",
            "run_id",
            "site_id",
            name="bookmark_staging_candidate_site_once_per_run",
        ),
        CheckConstraint("site_version > 0", name="positive_site_version"),
    )

    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    site_id: Mapped[str] = mapped_column(String(36), nullable=False)
    site_version: Mapped[int] = mapped_column(Integer, nullable=False)
    matched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
