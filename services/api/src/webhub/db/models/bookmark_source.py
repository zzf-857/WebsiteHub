"""书签导入的源事实表（发布后仍保留，用于溯源）。"""

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
