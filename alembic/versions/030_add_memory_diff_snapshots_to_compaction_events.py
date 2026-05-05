"""Add memory-diff snapshots and pending/completed status to compaction_events.

Each compaction run now records:

- ``min_message_seq``: lowest ``messages.seq`` covered by this event (the
  highest is already in ``max_message_seq``).
- ``status``: ``'pending'`` while the async LLM call is in flight,
  ``'completed'`` once it lands. Legacy rows default to ``'completed'``.
- Eight envelope-encrypted text columns: before/after snapshots of the
  four memory files the compaction LLM touches (memory, history, user,
  soul). Admins can read these via the premium shared-data endpoint
  to see what each compaction event actually distilled into MEMORY.md.

All new columns are nullable so existing rows remain readable. The
``status`` column has a server-side default of ``'completed'`` so the
NOT NULL constraint can be enforced without a backfill (existing rows
ran the prior synchronous-write path and are effectively complete).

Revision ID: 030
Revises: 029
Create Date: 2026-05-05
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "030"
down_revision: str = "029"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "compaction_events",
        sa.Column("min_message_seq", sa.Integer(), nullable=True),
    )
    op.add_column(
        "compaction_events",
        sa.Column(
            "status",
            sa.String(),
            nullable=False,
            server_default="completed",
        ),
    )
    for col in (
        "memory_text_before",
        "memory_text_after",
        "history_text_before",
        "history_text_after",
        "user_text_before",
        "user_text_after",
        "soul_text_before",
        "soul_text_after",
    ):
        op.add_column(
            "compaction_events",
            sa.Column(col, sa.Text(), nullable=True),
        )


def downgrade() -> None:
    for col in (
        "soul_text_after",
        "soul_text_before",
        "user_text_after",
        "user_text_before",
        "history_text_after",
        "history_text_before",
        "memory_text_after",
        "memory_text_before",
        "status",
        "min_message_seq",
    ):
        op.drop_column("compaction_events", col)
