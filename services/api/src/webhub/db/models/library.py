"""资料库主体：分类、标签、站点，以及站点的来源与向量。"""

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
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from webhub.db.models._base import Base, new_id, utc_now


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
    icon: Mapped[str] = mapped_column(
        String(32), nullable=False, default="Folder", server_default="Folder"
    )
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
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    favicon_url: Mapped[str | None] = mapped_column(Text)
    # 抓取到的 og:image / twitter:image；没抓到就是 None，前端不渲染预览位。
    preview_url: Mapped[str | None] = mapped_column(Text)
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
