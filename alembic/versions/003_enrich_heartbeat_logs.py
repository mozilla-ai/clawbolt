"""Add enriched fields to heartbeat_logs.

Revision ID: 003
Revises: 002
Create Date: 2026-03-23
"""

import sqlalchemy as sa

from alembic import op

revision: str = "003"
down_revision: str = "002"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column("heartbeat_logs", sa.Column("action_type", sa.String(), server_default="send"))
    op.add_column("heartbeat_logs", sa.Column("message_text", sa.Text(), server_default=""))
    op.add_column("heartbeat_logs", sa.Column("channel", sa.String(), server_default=""))
    op.add_column("heartbeat_logs", sa.Column("reasoning", sa.Text(), server_default=""))
    op.add_column("heartbeat_logs", sa.Column("tasks", sa.Text(), server_default=""))


def downgrade() -> None:
    op.drop_column("heartbeat_logs", "tasks")
    op.drop_column("heartbeat_logs", "reasoning")
    op.drop_column("heartbeat_logs", "channel")
    op.drop_column("heartbeat_logs", "message_text")
    op.drop_column("heartbeat_logs", "action_type")
