"""Add enabled column to channel_routes.

Allows users to selectively disable channels. Existing routes default
to enabled=True so current behavior is preserved.

Revision ID: 008
Revises: 007
Create Date: 2026-03-25
"""

import sqlalchemy as sa

from alembic import op

revision: str = "008"
down_revision: str = "007"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "channel_routes",
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )


def downgrade() -> None:
    op.drop_column("channel_routes", "enabled")
