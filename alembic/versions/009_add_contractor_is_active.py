"""Add is_active column to contractors table

Revision ID: 009
Revises: 008
Create Date: 2026-03-04

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "009"
down_revision: str | None = "008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "contractors",
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("1"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("contractors", "is_active")
