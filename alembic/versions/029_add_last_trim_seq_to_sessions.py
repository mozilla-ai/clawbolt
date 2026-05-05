"""Add ``sessions.last_trim_seq`` for the per-session trim watermark.

When the agent loop's trim path drops messages from LLM context (turn-cap
or token-budget driven), it advances this watermark to the highest dropped
``messages.seq``. ``load_conversation_history`` then filters to
``seq > last_trim_seq``, so the trimmed rows are no longer fed to the LLM
on subsequent inbounds. Without this, the agent loop would reload the full
DB history every message and re-trigger compaction every turn.

NULL on existing sessions (nothing has been trimmed yet); the load path
treats NULL as no filter, preserving today's behavior until the first
trim writes a value.

Revision ID: 029
Revises: 028
Create Date: 2026-05-05
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "029"
down_revision: str = "028"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("last_trim_seq", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sessions", "last_trim_seq")
