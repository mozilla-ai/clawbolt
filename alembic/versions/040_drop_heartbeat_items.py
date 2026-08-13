"""Drop the orphaned ``heartbeat_items`` table.

Revision 001 created ``heartbeat_items`` to back a structured
add/list/remove CRUD surface. PR #662 replaced that surface with a
freeform markdown textarea and made ``users.heartbeat_text`` the sole
source of truth: the ``HeartbeatItem`` model, DTOs, schemas, agent tools
and router all went away, but the table itself was never dropped.

The result is a table the migration chain creates and no model backs, so
``alembic revision --autogenerate`` emits ``drop_table('heartbeat_items')``
into every new migration and it has to be edited out by hand. Dropping it
here makes the chain and the models agree again.

Any remaining rows are pre-#662 structured items whose content was not
carried over into ``users.heartbeat_text``; they have been unreachable
since that refactor shipped.

Revision ID: 040
Revises: 039
Create Date: 2026-08-13
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "040"
down_revision: str | None = "039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # IF EXISTS rather than ``op.drop_table`` because presence is
    # environment-dependent, which is the problem this revision closes:
    # the table is invisible to ``Base.metadata``, so a database built by
    # ``create_all`` against current models never gets it, while one
    # migrated from 001 has it. Deploys always run ``alembic upgrade
    # head`` and so land in the second case, which makes this defensive
    # rather than load-bearing; the databases that actually lack the
    # table are the test ones both repos build with ``create_all``.
    op.execute("DROP TABLE IF EXISTS heartbeat_items")


def downgrade() -> None:
    op.create_table(
        "heartbeat_items",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            index=True,
            nullable=False,
        ),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("schedule", sa.String(), server_default="30m"),
        sa.Column("active_hours", sa.String(), server_default=""),
        # Naive timestamps, matching 001. Revision 007 converted the rest of
        # the schema to timestamptz but skipped this table.
        sa.Column("last_triggered_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(), server_default="active"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
