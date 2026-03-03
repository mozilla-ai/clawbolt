"""Add tool_interactions_json column to messages

Revision ID: 003
Revises: 002
Create Date: 2026-03-03

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "003"
down_revision: str | None = "002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("tool_interactions_json", sa.Text(), server_default="", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("messages", "tool_interactions_json")
