"""Space 与成员关系。"""

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
