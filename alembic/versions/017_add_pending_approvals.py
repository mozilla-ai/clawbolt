"""Add pending_approvals table for orphan detection on worker restart.

The approval gate's in-memory PendingApproval dies with the worker process.
Persisting the request (tool_name, description, channel, chat_id) lets a
fresh worker find orphaned rows on startup and send the user a recovery
message instead of silently dropping the conversation.

Revision ID: 017
Revises: 016
Create Date: 2026-04-18
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "017"
down_revision: str = "016"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "pending_approvals",
        sa.Column("user_id", sa.String(), primary_key=True, nullable=False),
        sa.Column("tool_name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("channel", sa.String(), nullable=False, server_default=""),
        sa.Column("chat_id", sa.String(), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("pending_approvals")
