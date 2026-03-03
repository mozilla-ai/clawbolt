"""Add timezone column to contractors table

Revision ID: 003
Revises: 002
Create Date: 2026-03-03

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "006"
down_revision: str | None = "005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "contractors",
        sa.Column("timezone", sa.String(50), server_default="", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("contractors", "timezone")
