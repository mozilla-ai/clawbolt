"""Add disabled_sub_tools column to tool_configs.

Revision ID: 002
Revises: 001
Create Date: 2026-03-18
"""

import sqlalchemy as sa

from alembic import op

revision: str = "002"
down_revision: str = "001"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column("tool_configs", sa.Column("disabled_sub_tools", sa.Text(), server_default=""))


def downgrade() -> None:
    op.drop_column("tool_configs", "disabled_sub_tools")
