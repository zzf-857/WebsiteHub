"""Store the latest page text / README snapshot per site and index it (Q48).

Revision ID: 20261022_0023
Revises: 20261022_0022
Create Date: 2026-10-22

``site_contents`` keeps one row per site (``site_id`` is the primary key) and
is removed with its site through the same-account foreign key.  The FTS5 table
``site_content_search`` mirrors it through triggers, so every write path —
including the cascade on site deletion — keeps the index in sync.  Existing
sites are not backfilled: the snapshot appears on their next analysis.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261022_0023"
down_revision: str | None = "20261022_0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TRIGGERS = (
    """
    CREATE TRIGGER site_contents_search_insert AFTER INSERT ON site_contents BEGIN
        INSERT INTO site_content_search(user_id, site_id, content)
        VALUES (NEW.user_id, NEW.site_id, NEW.content);
    END
    """,
    """
    CREATE TRIGGER site_contents_search_update AFTER UPDATE ON site_contents BEGIN
        DELETE FROM site_content_search WHERE site_id = OLD.site_id;
        INSERT INTO site_content_search(user_id, site_id, content)
        VALUES (NEW.user_id, NEW.site_id, NEW.content);
    END
    """,
    """
    CREATE TRIGGER site_contents_search_delete AFTER DELETE ON site_contents BEGIN
        DELETE FROM site_content_search WHERE site_id = OLD.site_id;
    END
    """,
)
_TRIGGER_NAMES = (
    "site_contents_search_insert",
    "site_contents_search_update",
    "site_contents_search_delete",
)


def upgrade() -> None:
    op.create_table(
        "site_contents",
        sa.Column("site_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source IN ('page', 'readme')",
            name=op.f("ck_site_contents_valid_source"),
        ),
        sa.CheckConstraint(
            "char_count >= 0",
            name=op.f("ck_site_contents_non_negative_char_count"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "site_id"],
            ["sites.user_id", "sites.id"],
            name="site_content_site_same_account",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("site_id", name=op.f("pk_site_contents")),
    )
    op.execute(
        """
        CREATE VIRTUAL TABLE site_content_search USING fts5(
            user_id UNINDEXED,
            site_id UNINDEXED,
            content,
            tokenize = 'unicode61'
        )
        """
    )
    for statement in _TRIGGERS:
        op.execute(statement)


def downgrade() -> None:
    for name in _TRIGGER_NAMES:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    op.execute("DROP TABLE IF EXISTS site_content_search")
    op.drop_table("site_contents")
