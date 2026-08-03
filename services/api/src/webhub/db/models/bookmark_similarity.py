"""Deterministic same-site bookmark suggestions and user decisions."""

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


class BookmarkSimilarityCluster(Base):
    """An immutable, explainable suggestion derived from one parse run."""

    __tablename__ = "bookmark_similarity_clusters"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "run_id",
            "id",
            name="bookmark_similarity_cluster_run_identity",
        ),
        UniqueConstraint(
            "user_id",
            "run_id",
            "site_key",
            name="bookmark_similarity_cluster_site_key_per_run",
        ),
        ForeignKeyConstraint(
            ["user_id", "run_id"],
            ["bookmark_import_runs.user_id", "bookmark_import_runs.id"],
            name="bookmark_similarity_cluster_run_same_account",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["user_id", "run_id", "canonical_candidate_id"],
            [
                "bookmark_staging_candidates.user_id",
                "bookmark_staging_candidates.run_id",
                "bookmark_staging_candidates.id",
            ],
            name="bookmark_similarity_cluster_canonical_candidate_same_run",
            ondelete="CASCADE",
        ),
        CheckConstraint("length(site_key) BETWEEN 1 AND 255", name="valid_site_key"),
        CheckConstraint("length(ruleset_version) BETWEEN 1 AND 64", name="valid_ruleset_version"),
        CheckConstraint("length(display_host) BETWEEN 1 AND 255", name="valid_display_host"),
        CheckConstraint("length(canonical_url) BETWEEN 1 AND 16384", name="valid_canonical_url"),
        CheckConstraint("length(canonical_title) BETWEEN 1 AND 160", name="valid_canonical_title"),
        CheckConstraint(
            "canonical_source IN ('imported_homepage', 'derived_origin_root', "
            "'existing_library')",
            name="valid_canonical_source",
        ),
        CheckConstraint("confidence IN ('high', 'medium', 'low')", name="valid_confidence"),
        CheckConstraint("candidate_count >= 2", name="minimum_candidate_count"),
        CheckConstraint("occurrence_count >= candidate_count", name="valid_occurrence_count"),
        CheckConstraint(
            "keep_original_create_count >= 0 AND merge_create_count >= 0 "
            "AND merge_create_count <= keep_original_create_count",
            name="valid_create_counts",
        ),
        CheckConstraint("first_source_sequence > 0", name="positive_first_sequence"),
        Index(
            "ix_bookmark_similarity_clusters_user_run_sequence_id",
            "user_id",
            "run_id",
            "first_source_sequence",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    site_key: Mapped[str] = mapped_column(String(255), nullable=False)
    ruleset_version: Mapped[str] = mapped_column(String(64), nullable=False)
    display_host: Mapped[str] = mapped_column(String(255), nullable=False)
    canonical_candidate_id: Mapped[str | None] = mapped_column(String(36))
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_title: Mapped[str] = mapped_column(String(160), nullable=False)
    canonical_source: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_codes_json: Mapped[str] = mapped_column(Text, nullable=False)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False)
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False)
    keep_original_create_count: Mapped[int] = mapped_column(Integer, nullable=False)
    merge_create_count: Mapped[int] = mapped_column(Integer, nullable=False)
    first_source_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class BookmarkSimilarityClusterMember(Base):
    __tablename__ = "bookmark_similarity_cluster_members"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "run_id", "cluster_id"],
            [
                "bookmark_similarity_clusters.user_id",
                "bookmark_similarity_clusters.run_id",
                "bookmark_similarity_clusters.id",
            ],
            name="bookmark_similarity_member_cluster_same_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["user_id", "run_id", "candidate_id"],
            [
                "bookmark_staging_candidates.user_id",
                "bookmark_staging_candidates.run_id",
                "bookmark_staging_candidates.id",
            ],
            name="bookmark_similarity_member_candidate_same_run",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "user_id",
            "run_id",
            "candidate_id",
            name="bookmark_similarity_candidate_once_per_run",
        ),
        CheckConstraint("first_source_sequence > 0", name="positive_first_sequence"),
        Index(
            "ix_bookmark_similarity_members_user_run_cluster_sequence_id",
            "user_id",
            "run_id",
            "cluster_id",
            "first_source_sequence",
            "candidate_id",
        ),
    )

    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    cluster_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    first_source_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    is_canonical: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class BookmarkSimilarityDecisionState(Base):
    """Optimistic version for decisions without mutating the parse snapshot."""

    __tablename__ = "bookmark_similarity_decision_states"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "job_id", "run_id"],
            [
                "bookmark_import_runs.user_id",
                "bookmark_import_runs.job_id",
                "bookmark_import_runs.id",
            ],
            name="bookmark_similarity_decision_state_run_same_job",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "user_id",
            "run_id",
            name="bookmark_similarity_decision_state_run_once",
        ),
        CheckConstraint("version > 0", name="positive_version"),
    )

    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(36), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class BookmarkSimilarityDecision(Base):
    __tablename__ = "bookmark_similarity_decisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "run_id"],
            [
                "bookmark_similarity_decision_states.user_id",
                "bookmark_similarity_decision_states.run_id",
            ],
            name="bookmark_similarity_decision_state_same_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["user_id", "run_id", "cluster_id"],
            [
                "bookmark_similarity_clusters.user_id",
                "bookmark_similarity_clusters.run_id",
                "bookmark_similarity_clusters.id",
            ],
            name="bookmark_similarity_decision_cluster_same_run",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "decision IN ('merge_to_homepage', 'keep_originals')",
            name="valid_decision",
        ),
    )

    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    cluster_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


__all__ = [
    "BookmarkSimilarityCluster",
    "BookmarkSimilarityClusterMember",
    "BookmarkSimilarityDecision",
    "BookmarkSimilarityDecisionState",
]
