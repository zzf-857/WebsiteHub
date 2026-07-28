"""Per-field user intent for derived website metadata."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKeyConstraint, String, false
from sqlalchemy.orm import Mapped, mapped_column

from webhub.db.models._base import Base, utc_now


class SiteMetadataPreference(Base):
    """Remember an explicit user decision, including an intentional clear."""

    __tablename__ = "site_metadata_preferences"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "site_id"],
            ["sites.user_id", "sites.id"],
            name="site_metadata_preference_site_same_account",
            ondelete="CASCADE",
        ),
    )

    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    site_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    summary_is_manual: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=false(),
    )
    description_is_manual: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    favicon_is_manual: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    category_is_manual: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    tags_are_manual: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Source is tracked per semantic field. A single completion timestamp
    # cannot prove that a preserved legacy value was produced by the model.
    summary_is_llm: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=false(),
    )
    description_is_llm: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    category_is_llm: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    tags_are_llm: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # A successful HTML pass may legitimately find no og:image. Recording that
    # fact avoids paying for the same preview-only retry on every later run.
    preview_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set only after all LLM tools produced a complete draft and the
    # guarded atomic write committed. It also identifies fields that a later
    # LLM re-analysis may replace unless the user has since made them manual.
    llm_analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


__all__ = ["SiteMetadataPreference"]
