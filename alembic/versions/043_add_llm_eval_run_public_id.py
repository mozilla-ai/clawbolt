"""Give ``llm_eval_runs`` a public id.

A run's report is a page an operator bookmarks, revisits, and pastes to
someone else, so it needs an address that is stable and not a row counter.
The integer primary key stays as the internal key the worker and the turn
rows use; only the API and the report URL move to this column.

Added nullable, backfilled per row, then tightened to NOT NULL so the unique
index cannot land on a table where every existing run shares a default.

Revision ID: 043
Revises: 042
Create Date: 2026-09-02
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "043"
down_revision: str | None = "042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("llm_eval_runs", sa.Column("public_id", sa.String(length=36), nullable=True))

    # Postgres has ``gen_random_uuid()`` in pgcrypto, which is not guaranteed
    # to be installed, so the ids are generated here and applied per row.
    connection = op.get_bind()
    rows = connection.execute(sa.text("SELECT id FROM llm_eval_runs")).fetchall()
    for (run_id,) in rows:
        connection.execute(
            sa.text("UPDATE llm_eval_runs SET public_id = :pid WHERE id = :id"),
            {"pid": str(uuid.uuid4()), "id": run_id},
        )

    op.alter_column("llm_eval_runs", "public_id", nullable=False)
    op.create_index("ix_llm_eval_runs_public_id", "llm_eval_runs", ["public_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_llm_eval_runs_public_id", table_name="llm_eval_runs")
    op.drop_column("llm_eval_runs", "public_id")
