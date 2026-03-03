"""Add last_compacted_message_id to conversations

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
        "conversations",
        sa.Column("last_compacted_message_id", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("conversations", "last_compacted_message_id")
