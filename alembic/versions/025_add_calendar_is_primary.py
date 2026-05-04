"""Add is_primary flag to calendar_configs.

Mirrors the ``primary: true`` flag Google returns on the calendarList
entry that represents the user's own (default) calendar. The agent uses
it to disambiguate when the user has multiple enabled calendars and the
LLM omits ``calendar_id``, so we no longer surface "Multiple calendars
available. Please specify calendar_id." on every event the agent tries
to create for a contractor with crew sub-calendars.

Backfill heuristic for existing rows: mark ``calendar_id == 'primary'``
True (the OSS pre-multi-calendar default) and otherwise leave False. The
``update_calendar_config`` route resyncs from Google on next save and
sets the flag correctly for users who have moved past the legacy
default.

Revision ID: 025
Revises: 024
Create Date: 2026-05-03
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "025"
down_revision: str = "024"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "calendar_configs",
        sa.Column(
            "is_primary",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    # Best-effort backfill: the legacy single-calendar default uses the
    # literal ``"primary"`` magic alias, which Google routes to the user's
    # own primary calendar. Mark those rows True so the new disambiguation
    # logic agrees with the historical behavior.
    op.execute("UPDATE calendar_configs SET is_primary = TRUE WHERE calendar_id = 'primary'")


def downgrade() -> None:
    op.drop_column("calendar_configs", "is_primary")
