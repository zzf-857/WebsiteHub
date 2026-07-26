"""Store per-account site embeddings for semantic retrieval.

Revision ID: 20260727_0008
Revises: 20260727_0007
Create Date: 2026-07-27
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260727_0008"
down_revision: str | None = "20260727_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "site_embeddings",
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("site_id", sa.String(36), nullable=False),
        # Which model produced the vector.  Vectors from different models are
        # not comparable, so a model change must invalidate rather than mix.
        sa.Column("model", sa.String(160), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("vector", sa.LargeBinary(), nullable=False),
        # Digest of the text that was embedded.  Re-embedding is the expensive
        # part (it spends the user's quota), so an unchanged site is skipped.
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "site_id", name="site_embedding_identity"),
        sa.ForeignKeyConstraint(
            ["user_id", "site_id"],
            ["sites.user_id", "sites.id"],
            name="site_embedding_site_same_account",
            # Deleting a site takes its vector with it: an orphaned vector would
            # keep surfacing a row that no longer exists.
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("dimensions > 0", name="positive_dimensions"),
        sa.CheckConstraint("length(content_hash) = 64", name="valid_content_hash"),
    )
    op.create_index(
        "ix_site_embeddings_user_model",
        "site_embeddings",
        ["user_id", "model"],
    )


def downgrade() -> None:
    op.drop_index("ix_site_embeddings_user_model", table_name="site_embeddings")
    op.drop_table("site_embeddings")
