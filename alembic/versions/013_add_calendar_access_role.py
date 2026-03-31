"""Add access_role column to calendar_configs.

Stores the Google Calendar access role (owner/writer/reader/freeBusyReader)
so the agent can distinguish read-only from writable calendars.

Revision ID: 013
Revises: 012
Create Date: 2026-03-31
"""

import sqlalchemy as sa

from alembic import op

revision: str = "013"
down_revision: str = "012"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "calendar_configs",
        sa.Column("access_role", sa.String(), server_default="", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("calendar_configs", "access_role")
