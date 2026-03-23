"""Add heartbeat_max_daily column to users.

Revision ID: 005
Revises: 004
Create Date: 2026-03-23
"""

import sqlalchemy as sa

from alembic import op

revision: str = "005"
down_revision: str = "004"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("heartbeat_max_daily", sa.Integer(), server_default="0"))


def downgrade() -> None:
    op.drop_column("users", "heartbeat_max_daily")
