"""Create the account-scoped manual library and FTS projection.

Revision ID: 20260726_0002
Revises: 20260726_0001
Create Date: 2026-07-26
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision: str = "20260726_0002"
down_revision: str | None = "20260726_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _create_fts_projection() -> None:
    op.execute(
        """
        CREATE VIRTUAL TABLE site_search USING fts5(
            user_id UNINDEXED,
            site_id UNINDEXED,
            name,
            original_url,
            identity_url,
            description,
            category_name,
            tag_names,
            tokenize = 'unicode61'
        )
        """
    )
    op.execute(
        """
        CREATE TRIGGER sites_search_insert AFTER INSERT ON sites BEGIN
            INSERT INTO site_search(
                user_id, site_id, name, original_url, identity_url, description,
                category_name, tag_names
            ) VALUES (
                NEW.user_id,
                NEW.id,
                NEW.name,
                NEW.original_url,
                NEW.identity_url,
                NEW.description,
                (SELECT name FROM categories
                 WHERE user_id = NEW.user_id AND id = NEW.category_id),
                ''
            );
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER sites_search_update
        AFTER UPDATE OF name, original_url, identity_url, description, category_id ON sites BEGIN
            UPDATE site_search
            SET name = NEW.name,
                original_url = NEW.original_url,
                identity_url = NEW.identity_url,
                description = NEW.description,
                category_name = (
                    SELECT name FROM categories
                    WHERE user_id = NEW.user_id AND id = NEW.category_id
                )
            WHERE user_id = NEW.user_id AND site_id = NEW.id;
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER sites_search_delete AFTER DELETE ON sites BEGIN
            DELETE FROM site_search
            WHERE user_id = OLD.user_id AND site_id = OLD.id;
        END
        """
    )
    op.execute(
        """
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
    )
    tag_projection = """
        COALESCE((
            SELECT group_concat(name, ' ')
            FROM (
                SELECT tags.name AS name
                FROM site_tags
                JOIN tags
                  ON tags.user_id = site_tags.user_id
                 AND tags.id = site_tags.tag_id
                WHERE site_tags.user_id = {prefix}.user_id
                  AND site_tags.site_id = {prefix}.site_id
                ORDER BY tags.normalized_name, tags.id
            )
        ), '')
    """
    op.execute(
        f"""
        CREATE TRIGGER site_tags_search_insert AFTER INSERT ON site_tags BEGIN
            UPDATE site_search
            SET tag_names = {tag_projection.format(prefix="NEW")}
            WHERE user_id = NEW.user_id AND site_id = NEW.site_id;
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER site_tags_search_delete AFTER DELETE ON site_tags BEGIN
            UPDATE site_search
            SET tag_names = {tag_projection.format(prefix="OLD")}
            WHERE user_id = OLD.user_id AND site_id = OLD.site_id;
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER tags_search_rename AFTER UPDATE OF name ON tags BEGIN
            UPDATE site_search
            SET tag_names = COALESCE((
                SELECT group_concat(name, ' ')
                FROM (
                    SELECT related_tags.name AS name
                    FROM site_tags AS related_site_tags
                    JOIN tags AS related_tags
                      ON related_tags.user_id = related_site_tags.user_id
                     AND related_tags.id = related_site_tags.tag_id
                    WHERE related_site_tags.user_id = NEW.user_id
                      AND related_site_tags.site_id = site_search.site_id
                    ORDER BY related_tags.normalized_name, related_tags.id
                )
            ), '')
            WHERE user_id = NEW.user_id
              AND site_id IN (
                  SELECT site_id FROM site_tags
                  WHERE user_id = NEW.user_id AND tag_id = NEW.id
              );
        END
        """
    )


def upgrade() -> None:
    op.create_table(
        "categories",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("normalized_name", sa.String(length=80), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(name) BETWEEN 1 AND 80", name="ck_categories_valid_name_length"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_categories_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_categories"),
        sa.UniqueConstraint("user_id", "id", name="category_account_identity"),
        sa.UniqueConstraint("user_id", "normalized_name", name="category_name_per_user"),
    )
    op.create_index("ix_categories_user_id", "categories", ["user_id"], unique=False)
    op.create_index(
        "uq_categories_default_per_user",
        "categories",
        ["user_id"],
        unique=True,
        sqlite_where=sa.text("is_default = 1"),
    )
    op.create_table(
        "tags",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=40), nullable=False),
        sa.Column("normalized_name", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(name) BETWEEN 1 AND 40", name="ck_tags_valid_name_length"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_tags_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tags"),
        sa.UniqueConstraint("user_id", "id", name="tag_account_identity"),
        sa.UniqueConstraint("user_id", "normalized_name", name="tag_name_per_user"),
    )
    op.create_index("ix_tags_user_id", "tags", ["user_id"], unique=False)
    op.create_table(
        "sites",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("category_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("normalized_name", sa.String(length=160), nullable=False),
        sa.Column("original_url", sa.Text(), nullable=False),
        sa.Column("identity_url", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("favicon_url", sa.Text(), nullable=True),
        sa.Column("pinned", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("analysis_status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(name) BETWEEN 1 AND 160", name="ck_sites_valid_name_length"),
        sa.CheckConstraint("version > 0", name="ck_sites_positive_version"),
        sa.CheckConstraint(
            "source IN ('manual', 'agent', 'browser_import', 'backup')",
            name="ck_sites_valid_source",
        ),
        sa.CheckConstraint(
            "analysis_status IN ('not_analyzed', 'pending', 'complete', 'failed', 'limited')",
            name="ck_sites_valid_analysis_status",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_sites_user_id_users", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "category_id"],
            ["categories.user_id", "categories.id"],
            name="site_category_same_account",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sites"),
        sa.UniqueConstraint("user_id", "id", name="site_account_identity"),
        sa.UniqueConstraint("user_id", "identity_url", name="site_identity_per_user"),
    )
    op.create_index("ix_sites_user_category", "sites", ["user_id", "category_id"])
    op.create_index("ix_sites_user_created_id", "sites", ["user_id", "created_at", "id"])
    op.create_index("ix_sites_user_name_id", "sites", ["user_id", "normalized_name", "id"])
    op.create_index("ix_sites_user_pinned", "sites", ["user_id", "pinned"])
    op.create_index("ix_sites_user_updated_id", "sites", ["user_id", "updated_at", "id"])
    op.create_table(
        "site_tags",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("site_id", sa.String(length=36), nullable=False),
        sa.Column("tag_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id", "site_id"],
            ["sites.user_id", "sites.id"],
            name="site_tag_site_same_account",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "tag_id"],
            ["tags.user_id", "tags.id"],
            name="site_tag_tag_same_account",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "site_id", "tag_id", name="pk_site_tags"),
    )
    op.create_index("ix_site_tags_user_tag_site", "site_tags", ["user_id", "tag_id", "site_id"])

    bind = op.get_bind()
    now = datetime.now(UTC)
    for user_id in bind.execute(sa.text("SELECT id FROM users")).scalars():
        bind.execute(
            sa.text(
                """
                INSERT INTO categories(
                    id, user_id, name, normalized_name, is_default, created_at, updated_at
                ) VALUES (
                    :id, :user_id, :name, :normalized_name, 1, :created_at, :updated_at
                )
                """
            ),
            {
                "id": str(uuid4()),
                "user_id": user_id,
                "name": "未分类",
                "normalized_name": "未分类",
                "created_at": now,
                "updated_at": now,
            },
        )

    _create_fts_projection()


def downgrade() -> None:
    for trigger in (
        "tags_search_rename",
        "site_tags_search_delete",
        "site_tags_search_insert",
        "categories_search_rename",
        "sites_search_delete",
        "sites_search_update",
        "sites_search_insert",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    op.execute("DROP TABLE IF EXISTS site_search")
    op.drop_index("ix_site_tags_user_tag_site", table_name="site_tags")
    op.drop_table("site_tags")
    op.drop_index("ix_sites_user_updated_id", table_name="sites")
    op.drop_index("ix_sites_user_pinned", table_name="sites")
    op.drop_index("ix_sites_user_name_id", table_name="sites")
    op.drop_index("ix_sites_user_created_id", table_name="sites")
    op.drop_index("ix_sites_user_category", table_name="sites")
    op.drop_table("sites")
    op.drop_index("ix_tags_user_id", table_name="tags")
    op.drop_table("tags")
    op.drop_index("uq_categories_default_per_user", table_name="categories")
    op.drop_index("ix_categories_user_id", table_name="categories")
    op.drop_table("categories")
