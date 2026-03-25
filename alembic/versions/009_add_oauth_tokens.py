"""Add oauth_tokens table for database-backed token storage.

Moves OAuth token persistence from ephemeral filesystem to PostgreSQL so
tokens survive container redeployments. Sensitive columns (access_token,
refresh_token) are encrypted at rest when ENCRYPTION_KEY is configured.

Revision ID: 009
Revises: 008
Create Date: 2026-03-25
"""

import sqlalchemy as sa

from alembic import op

revision: str = "009"
down_revision: str = "008"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "oauth_tokens",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("integration", sa.String(), nullable=False),
        sa.Column("access_token", sa.Text(), nullable=False, server_default=""),
        sa.Column("refresh_token", sa.Text(), nullable=False, server_default=""),
        sa.Column("token_type", sa.String(), nullable=False, server_default="Bearer"),
        sa.Column("expires_at", sa.Float(), nullable=False, server_default="0"),
        sa.Column("scopes_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("realm_id", sa.String(), nullable=False, server_default=""),
        sa.Column("extra_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "integration", name="uq_oauth_token_user_integration"),
    )
    op.create_index("ix_oauth_tokens_user_id", "oauth_tokens", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_oauth_tokens_user_id", table_name="oauth_tokens")
    op.drop_table("oauth_tokens")
