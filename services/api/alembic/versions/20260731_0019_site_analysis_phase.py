"""Expose the live phase of one site analysis.

Revision ID: 20260731_0019
Revises: 20260731_0018
Create Date: 2026-07-31
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260731_0019"
down_revision: str | None = "20260731_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

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


def upgrade() -> None:
    op.add_column(
        "sites",
        sa.Column("analysis_phase", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    # Native SQLite DROP COLUMN avoids Alembic batch rebuilding `sites`; the
    # cross-table category trigger must be detached during that ALTER.
    op.execute("DROP TRIGGER IF EXISTS categories_search_rename")
    op.drop_column("sites", "analysis_phase")
    op.execute(_CATEGORIES_SEARCH_RENAME)
