"""Add disabled_tools column to calendar_configs.

Stores a JSON list of per-calendar tool names that are disabled,
e.g. '["calendar_create_event", "calendar_delete_event"]'.

Revision ID: 012
Revises: 011
Create Date: 2026-03-30
"""

import sqlalchemy as sa

from alembic import op

revision: str = "012"
down_revision: str = "011"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "calendar_configs",
        sa.Column("disabled_tools", sa.Text(), server_default="", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("calendar_configs", "disabled_tools")
