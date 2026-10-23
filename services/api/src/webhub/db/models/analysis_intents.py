"""Durable outstanding analysis requests for newly saved collections."""

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
)
from sqlalchemy.orm import Mapped, mapped_column

from webhub.db.models._base import Base, utc_now


class SiteAnalysisIntent(Base):
    __tablename__ = "site_analysis_intents"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "site_id"],
            ["sites.user_id", "sites.id"],
            name="site_analysis_intent_site_same_account",
            ondelete="CASCADE",
        ),
        CheckConstraint("intent IN ('site_enrichment')", name="site_analysis_intent_kind"),
        Index("ix_site_analysis_intents_next_attempt_at", "next_attempt_at"),
    )

    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    site_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("sites.id", ondelete="CASCADE"),
        primary_key=True,
    )
    intent: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="site_enrichment",
        server_default="site_enrichment",
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
    )


__all__ = ["SiteAnalysisIntent"]
