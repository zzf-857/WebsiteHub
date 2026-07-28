"""Separate derived site analysis time from the user-visible edit time.

Revision ID: 20260728_0010
Revises: 20260727_0009
Create Date: 2026-07-28
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260728_0010"
down_revision: str | None = "20260727_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Dropping a column through Alembic's SQLite batch mode rebuilds `sites`. The
# categories trigger selects from that table and makes the rename phase fail.
# Keep this migration on native ALTER TABLE and temporarily remove only that
# cross-table trigger during downgrade, matching 52c3f6173b38.
_CATEGORIES_SEARCH_RENAME = """
CREATE TRIGGER categories_search_rename AFTER UPDATE OF name ON categories BEGIN
    UPDATE site_search
    SET category_name = NEW.name
    WHERE user_id = NEW.user_id
      AND site_id IN (
          SELECT id FROM sites
          WHERE user_id = NEW.user_id AND category_id = NEW.id
      );
END
"""

_INDEX_NAME = "ix_sites_user_analysis_status_updated_created_id"


def upgrade() -> None:
    op.add_column(
        "sites",
        sa.Column("analysis_updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        _INDEX_NAME,
        "sites",
        ["user_id", "analysis_status", "analysis_updated_at", "created_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name="sites")
    op.execute("DROP TRIGGER IF EXISTS categories_search_rename")
    op.drop_column("sites", "analysis_updated_at")
    op.execute(_CATEGORIES_SEARCH_RENAME)
