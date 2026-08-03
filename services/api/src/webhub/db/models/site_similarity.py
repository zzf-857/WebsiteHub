"""Durable, account-scoped snapshots for library similarity review."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from webhub.db.models._base import Base, new_id, utc_now


class SiteSimilarityScanRun(Base):
    __tablename__ = "site_similarity_scan_runs"
    __table_args__ = (
        UniqueConstraint("user_id", "id", name="site_similarity_run_account_identity"),
        CheckConstraint(
            "status IN ('ready', 'applied', 'superseded')",
            name="valid_status",
        ),
        CheckConstraint("length(ruleset_version) BETWEEN 1 AND 64", name="valid_ruleset_version"),
        CheckConstraint("length(library_fingerprint) = 64", name="valid_library_fingerprint"),
        CheckConstraint("site_count >= 0", name="nonnegative_site_count"),
        CheckConstraint("duplicate_group_count >= 0", name="nonnegative_duplicate_group_count"),
        CheckConstraint("same_site_group_count >= 0", name="nonnegative_same_site_group_count"),
        CheckConstraint("member_count >= 0", name="nonnegative_member_count"),
        CheckConstraint("version > 0", name="positive_version"),
        CheckConstraint(
            "(status = 'applied' AND applied_at IS NOT NULL AND result_json IS NOT NULL) OR "
            "(status != 'applied' AND applied_at IS NULL AND result_json IS NULL)",
            name="applied_result_consistency",
        ),
        Index(
            "uq_site_similarity_ready_run_per_user",
            "user_id",
            unique=True,
            sqlite_where=text("status = 'ready'"),
            postgresql_where=text("status = 'ready'"),
        ),
        Index(
            "ix_site_similarity_runs_user_created_id",
            "user_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ready")
    ruleset_version: Mapped[str] = mapped_column(String(64), nullable=False)
    library_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    site_count: Mapped[int] = mapped_column(Integer, nullable=False)
    duplicate_group_count: Mapped[int] = mapped_column(Integer, nullable=False)
    same_site_group_count: Mapped[int] = mapped_column(Integer, nullable=False)
    member_count: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    result_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SiteSimilarityGroup(Base):
    __tablename__ = "site_similarity_groups"
    __table_args__ = (
        UniqueConstraint("user_id", "run_id", "id", name="site_similarity_group_run_identity"),
        UniqueConstraint(
            "user_id",
            "run_id",
            "ordinal",
            name="site_similarity_group_ordinal_per_run",
        ),
        ForeignKeyConstraint(
            ["user_id", "run_id"],
            ["site_similarity_scan_runs.user_id", "site_similarity_scan_runs.id"],
            name="site_similarity_group_run_same_account",
            ondelete="CASCADE",
        ),
        CheckConstraint("kind IN ('duplicate', 'same_site')", name="valid_kind"),
        CheckConstraint("member_count >= 2", name="minimum_member_count"),
        CheckConstraint("ordinal >= 0", name="nonnegative_ordinal"),
        Index(
            "ix_site_similarity_groups_user_run_kind_ordinal",
            "user_id",
            "run_id",
            "kind",
            "ordinal",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    site_key: Mapped[str] = mapped_column(String(320), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    display_host: Mapped[str] = mapped_column(String(320), nullable=False)
    member_count: Mapped[int] = mapped_column(Integer, nullable=False)
    recommended_site_id: Mapped[str] = mapped_column(String(36), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class SiteSimilarityGroupMember(Base):
    __tablename__ = "site_similarity_group_members"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "run_id", "group_id"],
            [
                "site_similarity_groups.user_id",
                "site_similarity_groups.run_id",
                "site_similarity_groups.id",
            ],
            name="site_similarity_member_group_same_run",
            ondelete="CASCADE",
        ),
        CheckConstraint("expected_version > 0", name="positive_expected_version"),
        CheckConstraint("sort_order >= 0", name="nonnegative_sort_order"),
        Index(
            "ix_site_similarity_members_user_run_group_order",
            "user_id",
            "run_id",
            "group_id",
            "sort_order",
        ),
    )

    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    group_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    site_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    expected_version: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    original_url: Mapped[str] = mapped_column(Text, nullable=False)
    identity_url: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    favicon_url: Mapped[str | None] = mapped_column(Text)
    preview_url: Mapped[str | None] = mapped_column(Text)
    category_id: Mapped[str] = mapped_column(String(36), nullable=False)
    category_name: Mapped[str] = mapped_column(String(80), nullable=False)
    category_is_default: Mapped[bool] = mapped_column(Boolean, nullable=False)
    category_icon: Mapped[str] = mapped_column(String(32), nullable=False)
    tags_json: Mapped[str] = mapped_column(Text, nullable=False)
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    analysis_status: Mapped[str] = mapped_column(String(32), nullable=False)
    site_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    site_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)
    is_recommended: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class SiteSimilarityDecision(Base):
    __tablename__ = "site_similarity_decisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "run_id", "group_id"],
            [
                "site_similarity_groups.user_id",
                "site_similarity_groups.run_id",
                "site_similarity_groups.id",
            ],
            name="site_similarity_decision_group_same_run",
            ondelete="CASCADE",
        ),
    )

    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    group_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # The deterministic primary receiver for relationships from deleted members.
    # The complete keep set lives in SiteSimilarityDecisionMember.
    keep_site_id: Mapped[str | None] = mapped_column(String(36))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class SiteSimilarityDecisionMember(Base):
    __tablename__ = "site_similarity_decision_members"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "run_id", "group_id"],
            [
                "site_similarity_decisions.user_id",
                "site_similarity_decisions.run_id",
                "site_similarity_decisions.group_id",
            ],
            name="site_similarity_selected_decision_same_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["user_id", "run_id", "group_id", "site_id"],
            [
                "site_similarity_group_members.user_id",
                "site_similarity_group_members.run_id",
                "site_similarity_group_members.group_id",
                "site_similarity_group_members.site_id",
            ],
            name="site_similarity_selected_member_same_group",
            ondelete="CASCADE",
        ),
    )

    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    group_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    site_id: Mapped[str] = mapped_column(String(36), primary_key=True)


__all__ = [
    "SiteSimilarityDecision",
    "SiteSimilarityDecisionMember",
    "SiteSimilarityGroup",
    "SiteSimilarityGroupMember",
    "SiteSimilarityScanRun",
]
