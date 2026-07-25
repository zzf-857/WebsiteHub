"""Create the identity and account configuration kernel.

Revision ID: 20260726_0001
Revises:
Create Date: 2026-07-26
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260726_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("username", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=80), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("username", name=op.f("uq_users_username")),
    )
    op.create_table(
        "user_preferences",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("theme", sa.String(length=16), nullable=False),
        sa.Column("locale", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "theme IN ('system', 'light', 'dark')",
            name="ck_user_preferences_valid_theme",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_preferences_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_user_preferences")),
    )
    op.create_table(
        "login_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_login_sessions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_login_sessions")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_login_sessions_token_hash")),
    )
    op.create_index(
        "ix_login_sessions_user_active",
        "login_sessions",
        ["user_id", "revoked_at", "expires_at"],
        unique=False,
    )
    op.create_index(op.f("ix_login_sessions_user_id"), "login_sessions", ["user_id"], unique=False)
    op.create_table(
        "provider_configs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("provider", sa.String(length=48), nullable=False),
        sa.Column("display_name", sa.String(length=80), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=True),
        sa.Column("model_name", sa.String(length=160), nullable=True),
        sa.Column("secret_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("secret_nonce", sa.LargeBinary(), nullable=True),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("config_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('model', 'search', 'embedding')",
            name="ck_provider_configs_valid_kind",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_provider_configs_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider_configs")),
        sa.UniqueConstraint(
            "user_id",
            "kind",
            "display_name",
            name="provider_name_per_user_kind",
        ),
    )
    op.create_index(
        op.f("ix_provider_configs_user_id"), "provider_configs", ["user_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_provider_configs_user_id"), table_name="provider_configs")
    op.drop_table("provider_configs")
    op.drop_index(op.f("ix_login_sessions_user_id"), table_name="login_sessions")
    op.drop_index("ix_login_sessions_user_active", table_name="login_sessions")
    op.drop_table("login_sessions")
    op.drop_table("user_preferences")
    op.drop_table("users")
