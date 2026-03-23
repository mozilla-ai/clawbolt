"""Add initial_system_prompt column to sessions.

Revision ID: 004
Revises: 003
Create Date: 2026-03-23
"""

import sqlalchemy as sa

from alembic import op

revision: str = "004"
down_revision: str = "003"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("initial_system_prompt", sa.Text(), server_default=""))


def downgrade() -> None:
    op.drop_column("sessions", "initial_system_prompt")
