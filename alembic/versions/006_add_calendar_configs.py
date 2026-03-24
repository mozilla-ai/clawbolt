"""Add calendar_configs table.

Revision ID: 006
Revises: 005
Create Date: 2026-03-23
"""

import sqlalchemy as sa

from alembic import op

revision: str = "006"
down_revision: str = "005"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "calendar_configs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), server_default=""),
        sa.Column("calendar_id", sa.String(), server_default="primary"),
        sa.Column("enabled", sa.Boolean(), server_default="true"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("user_id", "provider", name="uq_calendar_config_user_provider"),
    )


def downgrade() -> None:
    op.drop_table("calendar_configs")
