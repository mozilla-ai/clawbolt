"""Add approval_events audit log.

Records every transition in the tool-approval lifecycle so admins can
see when the agent was blocked on a permission prompt, what tool it was
gating, and how the request resolved (approved, denied, timed out,
recovered after a worker crash, ...). The existing ``pending_approvals``
table only carries in-flight rows and is deleted on resolve, so it
cannot answer "what happened to that approval ten minutes ago?".

Append-only. No retention sweep ships with this migration; the table
is small (handful of rows per user per active session).

Revision ID: 028
Revises: 027
Create Date: 2026-05-05
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "028"
down_revision: str = "027"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "approval_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("tool_name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("channel", sa.String(), nullable=False, server_default=""),
        sa.Column("chat_id", sa.String(), nullable=False, server_default=""),
        sa.Column("decision", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_approval_events_user_id", "approval_events", ["user_id"])
    op.create_index("ix_approval_events_created_at", "approval_events", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_approval_events_created_at", "approval_events")
    op.drop_index("ix_approval_events_user_id", "approval_events")
    op.drop_table("approval_events")
