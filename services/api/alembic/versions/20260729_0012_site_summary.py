"""Add a short site summary with per-field ownership metadata.

Revision ID: 20260729_0012
Revises: 20260728_0011
Create Date: 2026-07-29
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260729_0012"
down_revision: str | None = "20260728_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# SQLite may rebuild `sites` while dropping a column. The cross-table category
# trigger selects from `sites` and otherwise makes the rename phase fail. This
# is the same guarded native-ALTER pattern used by 52c3f6173b38 and 0010.
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

_SUMMARY_LEGACY_COLUMN = "_webhub_0012_summary_legacy"


def _summary_column_is_strict(column: dict[str, object]) -> bool:
    default = column.get("default")
    normalized_default = str(default).strip().strip("()") if default is not None else ""
    return column.get("nullable") is False and normalized_default in {"''", '""'}


def _add_strict_summary_column() -> None:
    op.add_column(
        "sites",
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
    )


def _restore_legacy_summary() -> None:
    # A previous non-transactional attempt may have stopped after any one of
    # these statements. Prefer an already-written new value, otherwise recover
    # the old value before removing the private migration column.
    op.execute(
        f"""
        UPDATE sites
        SET summary = CASE
            WHEN summary IS NULL OR summary = ''
                THEN COALESCE({_SUMMARY_LEGACY_COLUMN}, '')
            ELSE summary
        END
        """
    )
    op.drop_column("sites", _SUMMARY_LEGACY_COLUMN)


def _ensure_strict_summary_column() -> None:
    columns = {
        column["name"]: column
        for column in sa.inspect(op.get_bind()).get_columns("sites")
    }
    summary = columns.get("summary")
    legacy_exists = _SUMMARY_LEGACY_COLUMN in columns

    if summary is not None and legacy_exists:
        _restore_legacy_summary()
        columns = {
            column["name"]: column
            for column in sa.inspect(op.get_bind()).get_columns("sites")
        }
        summary = columns.get("summary")
        legacy_exists = False

    if summary is None:
        _add_strict_summary_column()
        if legacy_exists:
            _restore_legacy_summary()
        return

    if _summary_column_is_strict(summary):
        op.execute("UPDATE sites SET summary = '' WHERE summary IS NULL")
        return

    # Some early local databases contain an unmanaged nullable `summary` dead
    # column. Native column operations preserve the table and its FTS triggers;
    # batch_alter_table would rebuild `sites` and is deliberately forbidden.
    op.execute(
        f"ALTER TABLE sites RENAME COLUMN summary TO {_SUMMARY_LEGACY_COLUMN}"
    )
    _add_strict_summary_column()
    _restore_legacy_summary()


def upgrade() -> None:
    # Direct ADD COLUMN is safe with the existing FTS triggers and gives old
    # rows the exact empty/non-manual state required for explicit LLM backfill.
    # Some early local databases contain an unmanaged nullable `summary` dead
    # column. SQLite DDL is non-transactional, so inspect each column and make
    # this additive upgrade resume cleanly instead of asking operators to stamp
    # a schema that was only partly upgraded.
    _ensure_strict_summary_column()

    inspector = sa.inspect(op.get_bind())
    preference_columns = {
        column["name"]
        for column in inspector.get_columns("site_metadata_preferences")
    }
    if "summary_is_manual" not in preference_columns:
        op.add_column(
            "site_metadata_preferences",
            sa.Column(
                "summary_is_manual",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )
    if "summary_is_llm" not in preference_columns:
        op.add_column(
            "site_metadata_preferences",
            sa.Column(
                "summary_is_llm",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )


def downgrade() -> None:
    op.drop_column("site_metadata_preferences", "summary_is_llm")
    op.drop_column("site_metadata_preferences", "summary_is_manual")
    op.execute("DROP TRIGGER IF EXISTS categories_search_rename")
    op.drop_column("sites", "summary")
    op.execute(_CATEGORIES_SEARCH_RENAME)
