"""Persistent bookkeeping for bounded site-metadata backfills.

The ingestion worker deliberately keeps only a very small in-memory queue.
These records own the user-requested batch snapshot instead: a restart can
resume it, and progress always has a fixed denominator.  Items intentionally
do not reference ``sites`` with a foreign key.  A website may be deleted while
its batch is running; retaining the item lets the worker record ``skipped``
without silently changing the progress total.
"""

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
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from webhub.db.models._base import Base, new_id, utc_now


class SiteMetadataBackfillRun(Base):
    """One account-scoped, persistent metadata backfill request."""

    __tablename__ = "site_metadata_backfill_runs"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "id",
            name="site_metadata_backfill_run_account_identity",
        ),
        ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="site_metadata_backfill_run_user_owner",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "state IN ('queued', 'running', 'completed', "
            "'completed_with_errors', 'failed')",
            name="valid_state",
        ),
        CheckConstraint("version > 0", name="positive_version"),
        CheckConstraint(
            "consecutive_provider_failures >= 0",
            name="nonnegative_provider_failures",
        ),
        CheckConstraint("total_count >= 0", name="nonnegative_total_count"),
        CheckConstraint(
            "queued_count >= 0 AND running_count >= 0 AND complete_count >= 0 "
            "AND limited_count >= 0 AND failed_count >= 0 AND skipped_count >= 0",
            name="nonnegative_progress_counts",
        ),
        CheckConstraint(
            "queued_count + running_count + complete_count + limited_count "
            "+ failed_count + skipped_count = total_count",
            name="progress_counts_match_total",
        ),
        CheckConstraint(
            "(state IN ('queued', 'running') AND completed_at IS NULL) OR "
            "(state IN ('completed', 'completed_with_errors', 'failed') "
            "AND completed_at IS NOT NULL)",
            name="terminal_completion_time",
        ),
        CheckConstraint(
            "state != 'running' OR lease_expires_at IS NOT NULL",
            name="running_has_lease",
        ),
        # This turns repeated clicks or requests from another tab into a join
        # on one durable task instead of two copies of the same outbound work.
        Index(
            "uq_site_metadata_backfill_runs_active_per_user",
            "user_id",
            unique=True,
            sqlite_where=text("state IN ('queued', 'running')"),
            postgresql_where=text("state IN ('queued', 'running')"),
        ),
        Index(
            "ix_site_metadata_backfill_runs_user_state_updated_id",
            "user_id",
            "state",
            "updated_at",
            "id",
        ),
        Index(
            "ix_site_metadata_backfill_runs_lease_expiry",
            "state",
            "lease_expires_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    # Set once while snapshotting the eligible rows. Item deletion is never
    # cascaded from sites, so this stays a stable progress denominator.
    total_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    queued_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    running_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    complete_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    limited_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    lease_token_hash: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # A capability/authentication failure is a batch property. Persisting the
    # fuse prevents expired running items from retrying the same Provider after
    # a process crash.
    stop_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Retryable Provider failures are a run property, not thousands of
    # independent site failures. A successful model call resets the streak;
    # failures add a persisted cooldown and eventually trip stop_requested.
    consecutive_provider_failures: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    provider_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SiteMetadataBackfillItem(Base):
    """One immutable target from a run's website snapshot."""

    __tablename__ = "site_metadata_backfill_items"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "id",
            name="site_metadata_backfill_item_account_identity",
        ),
        UniqueConstraint(
            "user_id",
            "run_id",
            "site_id",
            name="site_metadata_backfill_item_once_per_run",
        ),
        ForeignKeyConstraint(
            ["user_id", "run_id"],
            [
                "site_metadata_backfill_runs.user_id",
                "site_metadata_backfill_runs.id",
            ],
            name="site_metadata_backfill_item_run_same_account",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "state IN ('queued', 'running', 'complete', 'limited', 'failed', 'skipped')",
            name="valid_state",
        ),
        CheckConstraint(
            "initial_analysis_status IN "
            "('not_analyzed', 'pending', 'complete', 'failed', 'limited')",
            name="valid_initial_analysis_status",
        ),
        CheckConstraint("expected_version > 0", name="positive_expected_version"),
        CheckConstraint("attempt_count >= 0", name="nonnegative_attempt_count"),
        CheckConstraint(
            "length(origin_key) BETWEEN 1 AND 320",
            name="valid_origin_key_length",
        ),
        CheckConstraint(
            "(state IN ('queued', 'running') AND completed_at IS NULL) OR "
            "(state IN ('complete', 'limited', 'failed', 'skipped') "
            "AND completed_at IS NOT NULL)",
            name="terminal_completion_time",
        ),
        CheckConstraint(
            "state != 'running' OR lease_expires_at IS NOT NULL",
            name="running_has_lease",
        ),
        Index(
            "ix_site_metadata_backfill_items_user_run_state_created_id",
            "user_id",
            "run_id",
            "state",
            "created_at",
            "id",
        ),
        Index(
            "ix_site_metadata_backfill_items_user_state_lease_id",
            "user_id",
            "state",
            "lease_expires_at",
            "id",
        ),
        Index(
            "ix_site_metadata_backfill_items_user_origin_state_id",
            "user_id",
            "origin_key",
            "state",
            "id",
        ),
        # The scheduler also checks this before claiming, but the database is
        # the final authority if a future non-SQLite deployment permits two
        # consumers to race between the read and conditional state update.
        Index(
            "uq_site_metadata_backfill_items_running_origin_per_run",
            "user_id",
            "run_id",
            "origin_key",
            unique=True,
            sqlite_where=text("state = 'running'"),
            postgresql_where=text("state = 'running'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    # Deliberately not a Site FK: deletion is a terminal skipped outcome, not a
    # reason to erase work that has already contributed to the progress total.
    site_id: Mapped[str] = mapped_column(String(36), nullable=False)
    expected_version: Mapped[int] = mapped_column(Integer, nullable=False)
    initial_analysis_status: Mapped[str] = mapped_column(String(32), nullable=False)
    # Frozen at run creation so a later metadata-only retry does not spend a
    # second LLM call after the three-tool enrichment already succeeded.
    requires_llm: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Canonical scheme + host + port is enough for a scheduler to enforce
    # same-origin fairness without reparsing every original URL on recovery.
    origin_key: Mapped[str] = mapped_column(String(320), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # The Site pending token owned by this item. Retaining it across an expired
    # item lease lets a restarted worker reclaim only its own abandoned claim.
    analysis_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token_hash: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # A fresh foreground/automatic claim is a temporary conflict, not a skip.
    # Delaying just that row lets the durable run continue with other origins.
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


__all__ = ["SiteMetadataBackfillItem", "SiteMetadataBackfillRun"]
