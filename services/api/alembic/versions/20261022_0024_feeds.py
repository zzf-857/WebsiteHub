"""Add RSS / podcast subscriptions: ``feeds`` and ``feed_items`` (Q50).

Revision ID: 20261022_0024
Revises: 20261022_0023
Create Date: 2026-10-22

Two new tables only; nothing existing is rebuilt.  ``feed_items.saved_site_id``
references ``sites.id`` with ``SET NULL`` so deleting a saved resource merely
unlinks the stream item.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261022_0024"
down_revision: str | None = "20261022_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "feeds",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("site_url", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("last_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_fetch_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("etag", sa.String(length=256), nullable=True),
        sa.Column("last_modified", sa.String(length=128), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('rss', 'podcast')", name=op.f("ck_feeds_valid_feed_kind")),
        sa.CheckConstraint("consecutive_failures >= 0", name=op.f("ck_feeds_valid_feed_failures")),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_feeds_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feeds")),
        sa.UniqueConstraint("user_id", "id", name="feed_account_identity"),
        sa.UniqueConstraint("user_id", "url", name="feed_url_per_user"),
    )
    op.create_index(
        "ix_feeds_enabled_next_fetch", "feeds", ["enabled", "next_fetch_at"], unique=False
    )
    op.create_table(
        "feed_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("feed_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("guid", sa.String(length=512), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("author", sa.String(length=200), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("audio_url", sa.Text(), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("saved_site_id", sa.String(length=36), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id", "feed_id"],
            ["feeds.user_id", "feeds.id"],
            name="feed_item_same_account",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["saved_site_id"],
            ["sites.id"],
            name=op.f("fk_feed_items_saved_site_id_sites"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_feed_items_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feed_items")),
        sa.UniqueConstraint("feed_id", "guid", name="feed_item_guid_per_feed"),
    )
    op.create_index("ix_feed_items_fetched", "feed_items", ["fetched_at"], unique=False)
    op.create_index(
        "ix_feed_items_user_published",
        "feed_items",
        ["user_id", "published_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_feed_items_user_published", table_name="feed_items")
    op.drop_index("ix_feed_items_fetched", table_name="feed_items")
    op.drop_table("feed_items")
    op.drop_index("ix_feeds_enabled_next_fetch", table_name="feeds")
    op.drop_table("feeds")
