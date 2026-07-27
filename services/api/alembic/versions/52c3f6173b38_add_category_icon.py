"""Add a category icon name and a scraped preview image column.

Revision ID: 52c3f6173b38
Revises: 20260727_0008
Create Date: 2026-07-27
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "52c3f6173b38"
down_revision: str | None = "20260727_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 20260727_0007 同一个理由，**不要**用 batch_alter_table：在 SQLite 上它会重建表
# （建临时表 → 拷数据 → 删原表 → 改名），而 20260726_0002 建的 categories_search_rename
# 触发器在 body 里 SELECT sites，会在 RENAME 期间对一个已不存在的 `sites` 触发，
# 直接报 `no such table: main.sites`。
#
# 坑在于 upgrade 会假装成功：batch 模式下纯 add_column 被优化成普通 ADD COLUMN、
# 不重建表，所以只有 downgrade 的 drop_column 会炸。`alembic upgrade head` 全绿
# 并不代表迁移是对的——必须跑 tests/test_migrations.py 那两个 round-trip 测试
# （upgrade → check → downgrade → base）才会暴露。
#
# 所以 upgrade 直接 add_column；downgrade 先摘掉那一个引用 sites 的触发器，
# 删完列再原样装回去。tags_search_rename 只碰 site_tags/tags，不受影响，不动它。

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
        "categories",
        sa.Column("icon", sa.String(length=32), nullable=False, server_default="Folder"),
    )
    op.add_column("sites", sa.Column("preview_url", sa.Text(), nullable=True))


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS categories_search_rename")
    op.drop_column("sites", "preview_url")
    op.drop_column("categories", "icon")
    op.execute(_CATEGORIES_SEARCH_RENAME)
