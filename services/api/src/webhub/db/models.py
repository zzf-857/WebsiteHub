from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from webhub.db.base import Base

DEFAULT_CATEGORY_NAME = "未分类"


def new_id() -> str:
    return str(uuid4())


def utc_now() -> datetime:
    return datetime.now(UTC)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(80), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )

    preferences: Mapped[UserPreference] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False
    )
    sessions: Mapped[list[LoginSession]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class UserPreference(Base):
    __tablename__ = "user_preferences"
    __table_args__ = (CheckConstraint("theme IN ('system', 'light', 'dark')", name="valid_theme"),)

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    theme: Mapped[str] = mapped_column(String(16), nullable=False, default="system")
    locale: Mapped[str] = mapped_column(String(16), nullable=False, default="zh-CN")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )

    user: Mapped[User] = relationship(back_populates="preferences")


class LoginSession(Base):
    __tablename__ = "login_sessions"
    __table_args__ = (
        Index("ix_login_sessions_user_active", "user_id", "revoked_at", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="sessions")


class ProviderConfig(Base):
    __tablename__ = "provider_configs"
    __table_args__ = (
        UniqueConstraint("user_id", "kind", "display_name", name="provider_name_per_user_kind"),
        CheckConstraint("kind IN ('model', 'search', 'embedding')", name="valid_kind"),
        Index(
            "uq_provider_configs_enabled_per_user_kind",
            "user_id",
            "kind",
            unique=True,
            sqlite_where=text("enabled = 1"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str] = mapped_column(String(48), nullable=False)
    display_name: Mapped[str] = mapped_column(String(80), nullable=False)
    base_url: Mapped[str | None] = mapped_column(Text)
    model_name: Mapped[str | None] = mapped_column(String(160))
    secret_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    secret_nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    config_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (
        UniqueConstraint("user_id", "id", name="category_account_identity"),
        UniqueConstraint("user_id", "normalized_name", name="category_name_per_user"),
        CheckConstraint("length(name) BETWEEN 1 AND 80", name="valid_name_length"),
        Index(
            "uq_categories_default_per_user",
            "user_id",
            unique=True,
            sqlite_where=text("is_default = 1"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(80), nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class Tag(Base):
    __tablename__ = "tags"
    __table_args__ = (
        UniqueConstraint("user_id", "id", name="tag_account_identity"),
        UniqueConstraint("user_id", "normalized_name", name="tag_name_per_user"),
        CheckConstraint("length(name) BETWEEN 1 AND 40", name="valid_name_length"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(40), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class Site(Base):
    __tablename__ = "sites"
    __table_args__ = (
        UniqueConstraint("user_id", "id", name="site_account_identity"),
        UniqueConstraint("user_id", "identity_url", name="site_identity_per_user"),
        ForeignKeyConstraint(
            ["user_id", "category_id"],
            ["categories.user_id", "categories.id"],
            name="site_category_same_account",
            ondelete="RESTRICT",
        ),
        CheckConstraint("length(name) BETWEEN 1 AND 160", name="valid_name_length"),
        CheckConstraint("version > 0", name="positive_version"),
        CheckConstraint(
            "source IN ('manual', 'agent', 'browser_import', 'backup')",
            name="valid_source",
        ),
        CheckConstraint(
            "analysis_status IN ('not_analyzed', 'pending', 'complete', 'failed', 'limited')",
            name="valid_analysis_status",
        ),
        Index("ix_sites_user_updated_id", "user_id", "updated_at", "id"),
        Index("ix_sites_user_created_id", "user_id", "created_at", "id"),
        Index("ix_sites_user_name_id", "user_id", "normalized_name", "id"),
        Index("ix_sites_user_category", "user_id", "category_id"),
        Index("ix_sites_user_pinned", "user_id", "pinned"),
        # 唯一索引而不是表约束：SQLite 上加表约束要重建整表，
        # 而 FTS 触发器引用了 sites，重建时会炸。见 20260727_0007 迁移。
        Index(
            "ix_sites_user_category_position",
            "user_id",
            "category_id",
            "position",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    category_id: Mapped[str] = mapped_column(String(36), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(160), nullable=False)
    original_url: Mapped[str] = mapped_column(Text, nullable=False)
    identity_url: Mapped[str] = mapped_column(Text, nullable=False)
    # 分类内的自定义顺序；同一 (user_id, category_id) 下唯一。
    position: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    favicon_url: Mapped[str | None] = mapped_column(Text)
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    analysis_status: Mapped[str] = mapped_column(String(32), nullable=False, default="not_analyzed")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class SiteTag(Base):
    __tablename__ = "site_tags"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "site_id"],
            ["sites.user_id", "sites.id"],
            name="site_tag_site_same_account",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["user_id", "tag_id"],
            ["tags.user_id", "tags.id"],
            name="site_tag_tag_same_account",
            ondelete="CASCADE",
        ),
        Index("ix_site_tags_user_tag_site", "user_id", "tag_id", "site_id"),
    )

    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    site_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tag_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class Space(Base):
    __tablename__ = "spaces"
    __table_args__ = (
        UniqueConstraint("user_id", "id", name="space_account_identity"),
        UniqueConstraint("user_id", "normalized_name", name="space_name_per_user"),
        CheckConstraint("length(name) BETWEEN 1 AND 120", name="valid_name_length"),
        CheckConstraint("version > 0", name="positive_version"),
        Index("ix_spaces_user_updated_id", "user_id", "updated_at", "id"),
        Index("ix_spaces_user_created_id", "user_id", "created_at", "id"),
        Index("ix_spaces_user_name_id", "user_id", "normalized_name", "id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class SpaceMember(Base):
    __tablename__ = "space_members"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "space_id"],
            ["spaces.user_id", "spaces.id"],
            name="space_member_space_same_account",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["user_id", "site_id"],
            ["sites.user_id", "sites.id"],
            name="space_member_site_same_account",
            ondelete="CASCADE",
        ),
        UniqueConstraint("user_id", "space_id", "position", name="space_position_per_space"),
        CheckConstraint("position >= 0", name="nonnegative_position"),
        Index(
            "ix_space_members_user_space_position_site",
            "user_id",
            "space_id",
            "position",
            "site_id",
        ),
        Index(
            "ix_space_members_user_site_space",
            "user_id",
            "site_id",
            "space_id",
        ),
    )

    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    space_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    site_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


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


class BookmarkSourceFolder(Base):
    __tablename__ = "bookmark_source_folders"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "snapshot_id", "id", name="bookmark_source_folder_snapshot_identity"
        ),
        UniqueConstraint(
            "user_id",
            "snapshot_id",
            "source_folder_key",
            name="bookmark_source_folder_source_key_per_snapshot",
        ),
        UniqueConstraint(
            "user_id",
            "snapshot_id",
            "source_sequence",
            name="bookmark_source_folder_sequence_per_snapshot",
        ),
        ForeignKeyConstraint(
            ["user_id", "snapshot_id"],
            ["bookmark_import_snapshots.user_id", "bookmark_import_snapshots.id"],
            name="bookmark_source_folder_snapshot_same_account",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["user_id", "snapshot_id", "parent_id"],
            [
                "bookmark_source_folders.user_id",
                "bookmark_source_folders.snapshot_id",
                "bookmark_source_folders.id",
            ],
            name="bookmark_source_folder_parent_same_snapshot",
            ondelete="CASCADE",
        ),
        CheckConstraint("length(source_folder_key) BETWEEN 1 AND 128", name="valid_source_key"),
        CheckConstraint("source_sequence > 0 AND source_order > 0", name="positive_order"),
        CheckConstraint("depth BETWEEN 1 AND 64", name="valid_depth"),
        CheckConstraint("length(title) <= 256", name="valid_title_length"),
        Index(
            "ix_bookmark_source_folders_user_snapshot_sequence_id",
            "user_id",
            "snapshot_id",
            "source_sequence",
            "id",
        ),
        Index(
            "ix_bookmark_source_folders_user_snapshot_parent_order_id",
            "user_id",
            "snapshot_id",
            "parent_id",
            "source_order",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    snapshot_id: Mapped[str] = mapped_column(String(36), nullable=False)
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


class BookmarkSourceOccurrence(Base):
    __tablename__ = "bookmark_source_occurrences"
    __table_args__ = (
        UniqueConstraint("user_id", "id", name="bookmark_source_occurrence_account_identity"),
        UniqueConstraint(
            "user_id",
            "snapshot_id",
            "id",
            name="bookmark_source_occurrence_snapshot_identity",
        ),
        UniqueConstraint(
            "user_id",
            "snapshot_id",
            "source_occurrence_key",
            name="bookmark_source_occurrence_source_key_per_snapshot",
        ),
        UniqueConstraint(
            "user_id",
            "snapshot_id",
            "source_sequence",
            name="bookmark_source_occurrence_sequence_per_snapshot",
        ),
        ForeignKeyConstraint(
            ["user_id", "snapshot_id"],
            ["bookmark_import_snapshots.user_id", "bookmark_import_snapshots.id"],
            name="bookmark_source_occurrence_snapshot_same_account",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["user_id", "snapshot_id", "folder_id"],
            [
                "bookmark_source_folders.user_id",
                "bookmark_source_folders.snapshot_id",
                "bookmark_source_folders.id",
            ],
            name="bookmark_source_occurrence_folder_same_snapshot",
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
            "ix_bookmark_source_occurrences_user_snapshot_sequence_id",
            "user_id",
            "snapshot_id",
            "source_sequence",
            "id",
        ),
        Index(
            "ix_bookmark_source_occurrences_user_snapshot_status_sequence",
            "user_id",
            "snapshot_id",
            "validation_status",
            "source_sequence",
        ),
        Index(
            "ix_bookmark_source_occurrences_user_snapshot_folder_order_id",
            "user_id",
            "snapshot_id",
            "folder_id",
            "source_order",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    snapshot_id: Mapped[str] = mapped_column(String(36), nullable=False)
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


class SiteImportOrigin(Base):
    __tablename__ = "site_import_origins"
    __table_args__ = (
        UniqueConstraint("user_id", "id", name="site_import_origin_account_identity"),
        UniqueConstraint(
            "user_id", "source_occurrence_id", name="site_import_origin_occurrence_once"
        ),
        ForeignKeyConstraint(
            ["user_id", "site_id"],
            ["sites.user_id", "sites.id"],
            name="site_import_origin_site_same_account",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["user_id", "source_occurrence_id"],
            ["bookmark_source_occurrences.user_id", "bookmark_source_occurrences.id"],
            name="site_import_origin_occurrence_same_account",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "link_action IN ('created_site', 'matched_existing')", name="valid_link_action"
        ),
        CheckConstraint("site_version_at_link > 0", name="positive_site_version"),
        Index(
            "ix_site_import_origins_user_site_created_id",
            "user_id",
            "site_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    site_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_occurrence_id: Mapped[str] = mapped_column(String(36), nullable=False)
    link_action: Mapped[str] = mapped_column(String(24), nullable=False)
    site_version_at_link: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class SiteEmbedding(Base):
    """派生缓存，不是记录本身：删掉可以从 sites 完整重建。"""

    __tablename__ = "site_embeddings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "site_id"],
            ["sites.user_id", "sites.id"],
            name="site_embedding_site_same_account",
            # 网站删了向量跟着走：留着的孤儿向量会一直把已不存在的行捞出来。
            ondelete="CASCADE",
        ),
        CheckConstraint("dimensions > 0", name="positive_dimensions"),
        CheckConstraint("length(content_hash) = 64", name="valid_content_hash"),
        Index("ix_site_embeddings_user_model", "user_id", "model"),
    )

    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    site_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # 不同模型产出的向量不可比较，所以换模型必须让缓存失效而不是混用。
    model: Mapped[str] = mapped_column(String(160), nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    vector: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )
