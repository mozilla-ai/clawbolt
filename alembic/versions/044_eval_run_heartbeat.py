"""Give an evaluation run a heartbeat, so the startup sweep can tell it is alive.

``mark_interrupted_runs`` flagged every row still ``running`` or ``pending``
at boot. That is only sound when one process ever runs, which a rolling deploy
breaks: the new instance boots while the old one drains, and the sweep marks a
run that is still advancing. Both concurrency guards in ``start_run`` key off
those statuses, so a duplicate run could then start for the same user while the
first was still calling the provider.

Nullable with no default. A row that never gets a heartbeat falls back to
``created_at`` in the sweep's predicate, which is the right answer for a
process that died between the insert and the first turn, and for every row
that predates this column.

Revision ID: 044
Revises: 043
Create Date: 2026-09-10
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "044"
down_revision: str | None = "043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "llm_eval_runs",
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("llm_eval_runs", "heartbeat_at")
