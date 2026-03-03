"""Add heartbeat_opt_in and heartbeat_frequency to contractors

Revision ID: 004
Revises: 003
Create Date: 2026-03-03

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "004"
down_revision: str | None = "003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "contractors",
        sa.Column(
            "heartbeat_opt_in",
            sa.Boolean(),
            server_default=sa.sql.expression.true(),
            nullable=False,
        ),
    )
    op.add_column(
        "contractors",
        sa.Column("heartbeat_frequency", sa.String(20), server_default="", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("contractors", "heartbeat_frequency")
    op.drop_column("contractors", "heartbeat_opt_in")
