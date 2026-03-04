"""Add role column to contractors

Revision ID: 002
Revises: 001
Create Date: 2026-03-04

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "002"
down_revision: str | None = "001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "contractors",
        sa.Column("role", sa.String(20), server_default="user", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("contractors", "role")
