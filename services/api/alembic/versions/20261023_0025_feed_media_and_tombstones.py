"""订阅条目墓碑与源、条目图片。

Revision ID: 20261023_0025
Revises: 20261022_0024
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261023_0025"
down_revision = "20261022_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("feed_items", sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("feed_items", sa.Column("image_url", sa.Text(), nullable=True))
    op.add_column("feeds", sa.Column("image_url", sa.Text(), nullable=True))
    op.create_index("ix_feed_items_user_purged", "feed_items", ["user_id", "purged_at"])


def downgrade() -> None:
    op.drop_index("ix_feed_items_user_purged", table_name="feed_items")
    op.drop_column("feeds", "image_url")
    op.drop_column("feed_items", "image_url")
    op.drop_column("feed_items", "purged_at")
