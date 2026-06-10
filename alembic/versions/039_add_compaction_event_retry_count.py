"""Add ``retry_count`` to ``compaction_events``.

The startup sweep in ``backend/app/agent/compaction_recovery.py`` retries
events stuck in ``'pending'`` (the async compaction LLM call crashed or
the process restarted mid-call). ``retry_count`` bounds those retries:
each attempt increments it before the LLM call, and rows at the cap stop
being selected so a poisoned range cannot retry forever.

Revision ID: 039
Revises: 038
Create Date: 2026-06-10
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "039"
down_revision: str | None = "038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "compaction_events",
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )


def downgrade() -> None:
    op.drop_column("compaction_events", "retry_count")
