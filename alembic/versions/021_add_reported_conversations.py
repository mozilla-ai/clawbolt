"""Add reported_conversations table for the /report channel command.

Item 5 of the clawbolt-premium privacy redesign (issue #325). Users on
iMessage/Telegram/SMS-only deployments need a way to flag a
conversation for admin review without ever opening the web app, so we
intercept the literal text ``/report [reason]`` in the inbound pipeline
and write a row here. The premium ``/admin/reported-conversations``
router consumes these rows.

Revision ID: 021
Revises: 020
Create Date: 2026-05-01
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "021"
down_revision: str = "020"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "reported_conversations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "session_id",
            sa.Integer(),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("anchor_seq", sa.Integer(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "reviewed_admin_user_id",
            sa.String(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_reported_conversations_user_id", "reported_conversations", ["user_id"])
    op.create_index(
        "ix_reported_conversations_session_id", "reported_conversations", ["session_id"]
    )
    op.create_index(
        "ix_reported_conversations_created_at", "reported_conversations", ["created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_reported_conversations_created_at", "reported_conversations")
    op.drop_index("ix_reported_conversations_session_id", "reported_conversations")
    op.drop_index("ix_reported_conversations_user_id", "reported_conversations")
    op.drop_table("reported_conversations")
