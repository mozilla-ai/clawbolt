"""Add heartbeat_checklist_items table

Revision ID: 002
Revises: 001
Create Date: 2026-02-28

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "002"
down_revision: str | None = "001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "heartbeat_checklist_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "contractor_id",
            sa.Integer(),
            sa.ForeignKey("contractors.id"),
            index=True,
            nullable=False,
        ),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("schedule", sa.String(50), server_default="daily", nullable=False),
        sa.Column("active_hours", sa.String(255), server_default="", nullable=False),
        sa.Column("last_triggered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(20), server_default="active", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("heartbeat_checklist_items")
