"""Allow the optional per-account code host (GitHub) Provider kind.

Revision ID: 20261022_0022
Revises: 20261021_0021
Create Date: 2026-10-22

SQLite cannot alter a CHECK constraint in place, so ``provider_configs`` is
rebuilt in batch mode.  The table has no triggers and nothing references it,
which makes the copy-and-rename safe.  The partial "one enabled config per
kind" index is dropped first and recreated explicitly afterwards so its
``WHERE enabled = 1`` clause never depends on index reflection.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261022_0022"
down_revision: str | None = "20261021_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Revision 0001 passed an already-prefixed name through the naming convention,
# so the stored constraint carries the prefix twice.  Keep that exact name.
_CONSTRAINT = "ck_provider_configs_ck_provider_configs_valid_kind"
_ENABLED_INDEX = "uq_provider_configs_enabled_per_user_kind"
_KINDS_BEFORE = "kind IN ('model', 'search', 'embedding')"
_KINDS_AFTER = "kind IN ('model', 'search', 'embedding', 'code_host')"


def _rebuild_kind_check(condition: str) -> None:
    op.drop_index(_ENABLED_INDEX, table_name="provider_configs")
    with op.batch_alter_table("provider_configs", recreate="always") as batch:
        batch.drop_constraint(op.f(_CONSTRAINT), type_="check")
        batch.create_check_constraint(op.f(_CONSTRAINT), condition)
    op.create_index(
        _ENABLED_INDEX,
        "provider_configs",
        ["user_id", "kind"],
        unique=True,
        sqlite_where=sa.text("enabled = 1"),
    )


def upgrade() -> None:
    _rebuild_kind_check(_KINDS_AFTER)


def downgrade() -> None:
    # A code host config cannot satisfy the narrower constraint; it is an
    # optional credential that the older schema has no place for.
    op.execute(sa.text("DELETE FROM provider_configs WHERE kind = 'code_host'"))
    _rebuild_kind_check(_KINDS_BEFORE)
