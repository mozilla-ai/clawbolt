"""Update ``heartbeat_max_daily`` default from 0 to 5, backfill existing rows.

The OSS ``User`` model defaulted ``heartbeat_max_daily`` to 0, which the
heartbeat scheduler interpreted as "use the global config default of 5".
Existing rows with 0 already behaved as 5 via that fallback. This migration
makes the intent explicit: new users default to 5, and existing users with
0 are updated to 5 so the DB value matches the actual behavior.

Revision ID: 038
Revises: 037
Create Date: 2026-05-24
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "038"
down_revision: str | None = "037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Update existing rows with heartbeat_max_daily = 0 to 5
    op.execute(sa.text("UPDATE users SET heartbeat_max_daily = 5 WHERE heartbeat_max_daily = 0"))
    # Change the column default so new rows get 5
    op.alter_column(
        "users",
        "heartbeat_max_daily",
        server_default=sa.text("5"),
    )


def downgrade() -> None:
    # Revert the default
    op.alter_column(
        "users",
        "heartbeat_max_daily",
        server_default=sa.text("0"),
    )
    # Revert existing rows back to 0 (the old default)
    op.execute(sa.text("UPDATE users SET heartbeat_max_daily = 0 WHERE heartbeat_max_daily = 5"))
