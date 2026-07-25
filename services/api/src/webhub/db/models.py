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
    secret_ciphertext: Mapped[bytes | None]
    secret_nonce: Mapped[bytes | None]
    key_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    config_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
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
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    favicon_url: Mapped[str | None] = mapped_column(Text)
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    analysis_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="not_analyzed"
    )
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
        UniqueConstraint(
            "user_id", "space_id", "position", name="space_position_per_space"
        ),
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
