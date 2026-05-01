"""Add compaction_events table for per-event compaction metrics.

Followup to the consent-gated admin viewer (clawbolt-premium #346 / #347).
The compaction summary line in ``backend/app/agent/compaction.py`` was
INFO-logged only, so admins debugging "why did this turn lose context?"
or "how often does this user compact?" had to grep Railway. This table
gives every compaction run a queryable row with sizes / costs / outcome
flags. The actual extracted content stays in MemoryDocument.history_text
(envelope-encrypted at rest); this table is metadata, not content.

Revision ID: 023
Revises: 022
Create Date: 2026-05-01
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "023"
down_revision: str = "022"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "compaction_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "triggered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("trimmed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("trimmed_chars", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_message_seq", sa.Integer(), nullable=True),
        sa.Column("memory_updated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("user_profile_updated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("soul_updated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("summary_len", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_compaction_events_user_id", "compaction_events", ["user_id"])
    op.create_index(
        "ix_compaction_events_triggered_at",
        "compaction_events",
        ["triggered_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_compaction_events_triggered_at", table_name="compaction_events")
    op.drop_index("ix_compaction_events_user_id", table_name="compaction_events")
    op.drop_table("compaction_events")
