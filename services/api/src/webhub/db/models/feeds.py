"""RSS / 播客订阅（Q50）：订阅源与条目。

条目只是“浏览流”，不进收藏库：保留 30 天后由后台任务清理；用户点“收进收藏库”
时才经正常建站流程写入 ``sites``，并在 ``saved_site_id`` 记下关联（该条目此后
不再被清理）。``saved_site_id`` 用单列外键 + ``SET NULL``：收藏被删时只断开
关联，不连带删除条目。
"""

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
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from webhub.db.models._base import Base, new_id, utc_now

FEED_KINDS = ("rss", "podcast")


class Feed(Base):
    __tablename__ = "feeds"
    __table_args__ = (
        UniqueConstraint("user_id", "id", name="feed_account_identity"),
        UniqueConstraint("user_id", "url", name="feed_url_per_user"),
        CheckConstraint("kind IN ('rss', 'podcast')", name="valid_feed_kind"),
        CheckConstraint("consecutive_failures >= 0", name="valid_feed_failures"),
        Index("ix_feeds_enabled_next_fetch", "enabled", "next_fetch_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="rss")
    site_url: Mapped[str | None] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_fetch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    etag: Mapped[str | None] = mapped_column(String(256))
    last_modified: Mapped[str | None] = mapped_column(String(128))
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_error: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class FeedItem(Base):
    __tablename__ = "feed_items"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "feed_id"],
            ["feeds.user_id", "feeds.id"],
            name="feed_item_same_account",
            ondelete="CASCADE",
        ),
        UniqueConstraint("feed_id", "guid", name="feed_item_guid_per_feed"),
        Index("ix_feed_items_user_published", "user_id", "published_at", "id"),
        Index("ix_feed_items_fetched", "fetched_at"),
        Index("ix_feed_items_user_purged", "user_id", "purged_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    feed_id: Mapped[str] = mapped_column(String(36), nullable=False)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    guid: Mapped[str] = mapped_column(String(512), nullable=False)
    url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    author: Mapped[str | None] = mapped_column(String(200))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    audio_url: Mapped[str | None] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(Text)
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    saved_site_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sites.id", ondelete="SET NULL")
    )
