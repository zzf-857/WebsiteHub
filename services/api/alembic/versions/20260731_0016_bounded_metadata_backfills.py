"""Bound metadata backfills and persist Provider stop diagnostics.

Revision ID: 20260731_0016
Revises: 20260731_0015
Create Date: 2026-07-31
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260731_0016"
down_revision: str | None = "20260731_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUNS = sa.table(
    "site_metadata_backfill_runs",
    sa.column("id", sa.String()),
    sa.column("user_id", sa.String()),
    sa.column("stop_requested", sa.Boolean()),
    sa.column("total_count", sa.Integer()),
    sa.column("queued_count", sa.Integer()),
    sa.column("running_count", sa.Integer()),
    sa.column("complete_count", sa.Integer()),
    sa.column("limited_count", sa.Integer()),
    sa.column("failed_count", sa.Integer()),
    sa.column("skipped_count", sa.Integer()),
)
_ITEMS = sa.table(
    "site_metadata_backfill_items",
    sa.column("user_id", sa.String()),
    sa.column("run_id", sa.String()),
    sa.column("state", sa.String()),
    sa.column("attempt_count", sa.Integer()),
)


def _reclassify_unattempted_fuse_items(*, source: str, target: str) -> None:
    connection = op.get_bind()
    affected_runs = list(
        connection.execute(
            sa.select(_ITEMS.c.user_id, _ITEMS.c.run_id)
            .select_from(
                _ITEMS.join(
                    _RUNS,
                    sa.and_(
                        _RUNS.c.user_id == _ITEMS.c.user_id,
                        _RUNS.c.id == _ITEMS.c.run_id,
                    ),
                )
            )
            .where(
                _RUNS.c.stop_requested.is_(True),
                _ITEMS.c.state == source,
                _ITEMS.c.attempt_count == 0,
            )
            .distinct()
        )
    )
    if not affected_runs:
        return

    for user_id, run_id in affected_runs:
        connection.execute(
            sa.update(_ITEMS)
            .where(
                _ITEMS.c.user_id == user_id,
                _ITEMS.c.run_id == run_id,
                _ITEMS.c.state == source,
                _ITEMS.c.attempt_count == 0,
            )
            .values(state=target)
        )
        counts = connection.execute(
            sa.select(
                sa.func.count().label("total"),
                *(
                    sa.func.sum(sa.case(((_ITEMS.c.state == state), 1), else_=0)).label(
                        f"{state}_count"
                    )
                    for state in (
                        "queued",
                        "running",
                        "complete",
                        "limited",
                        "failed",
                        "skipped",
                    )
                ),
            ).where(_ITEMS.c.user_id == user_id, _ITEMS.c.run_id == run_id)
        ).one()
        connection.execute(
            sa.update(_RUNS)
            .where(_RUNS.c.user_id == user_id, _RUNS.c.id == run_id)
            .values(
                total_count=int(counts.total),
                queued_count=int(counts.queued_count or 0),
                running_count=int(counts.running_count or 0),
                complete_count=int(counts.complete_count or 0),
                limited_count=int(counts.limited_count or 0),
                failed_count=int(counts.failed_count or 0),
                skipped_count=int(counts.skipped_count or 0),
            )
        )


def upgrade() -> None:
    op.add_column(
        "site_metadata_backfill_runs",
        sa.Column(
            "mode",
            sa.String(length=16),
            sa.CheckConstraint("mode IN ('metadata', 'full')", name="valid_mode"),
            nullable=False,
            server_default="full",
        ),
    )
    op.add_column(
        "site_metadata_backfill_runs",
        sa.Column(
            "stop_reason",
            sa.String(length=48),
            sa.CheckConstraint(
                "stop_reason IS NULL OR stop_reason IN ("
                "'provider_rate_limited', 'provider_temporary_failure', "
                "'provider_unavailable', 'internal_error')",
                name="valid_stop_reason",
            ),
            nullable=True,
        ),
    )
    _reclassify_unattempted_fuse_items(source="failed", target="skipped")


def downgrade() -> None:
    # The data repair is intentionally not reversed: older schemas already
    # support `skipped`, and changing every untouched skipped row back to
    # `failed` would also corrupt legitimate historical skip outcomes.
    op.drop_column("site_metadata_backfill_runs", "stop_reason")
    op.drop_column("site_metadata_backfill_runs", "mode")
