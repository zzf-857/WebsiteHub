"""Create account-scoped spaces and ordered memberships.

Revision ID: 20260726_0003
Revises: 20260726_0002
Create Date: 2026-07-26
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260726_0003"
down_revision: str | None = "20260726_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "spaces",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("normalized_name", sa.String(length=120), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(name) BETWEEN 1 AND 120", name="ck_spaces_valid_name_length"),
        sa.CheckConstraint("version > 0", name="ck_spaces_positive_version"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_spaces_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_spaces"),
        sa.UniqueConstraint("user_id", "id", name="space_account_identity"),
        sa.UniqueConstraint("user_id", "normalized_name", name="space_name_per_user"),
    )
    op.create_index("ix_spaces_user_id", "spaces", ["user_id"], unique=False)
    op.create_index("ix_spaces_user_updated_id", "spaces", ["user_id", "updated_at", "id"])
    op.create_index("ix_spaces_user_created_id", "spaces", ["user_id", "created_at", "id"])
    op.create_index("ix_spaces_user_name_id", "spaces", ["user_id", "normalized_name", "id"])

    op.create_table(
        "space_members",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("space_id", sa.String(length=36), nullable=False),
        sa.Column("site_id", sa.String(length=36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("position >= 0", name="ck_space_members_nonnegative_position"),
        sa.ForeignKeyConstraint(
            ["user_id", "space_id"],
            ["spaces.user_id", "spaces.id"],
            name="space_member_space_same_account",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "site_id"],
            ["sites.user_id", "sites.id"],
            name="space_member_site_same_account",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "space_id", "site_id", name="pk_space_members"),
        sa.UniqueConstraint("user_id", "space_id", "position", name="space_position_per_space"),
    )
    op.create_index(
        "ix_space_members_user_space_position_site",
        "space_members",
        ["user_id", "space_id", "position", "site_id"],
    )
    op.create_index(
        "ix_space_members_user_site_space",
        "space_members",
        ["user_id", "site_id", "space_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_space_members_user_site_space", table_name="space_members")
    op.drop_index("ix_space_members_user_space_position_site", table_name="space_members")
    op.drop_table("space_members")
    op.drop_index("ix_spaces_user_name_id", table_name="spaces")
    op.drop_index("ix_spaces_user_created_id", table_name="spaces")
    op.drop_index("ix_spaces_user_updated_id", table_name="spaces")
    op.drop_index("ix_spaces_user_id", table_name="spaces")
    op.drop_table("spaces")
