"""Collapse the sessions table to one row per user.

Clawbolt is a single-conversation-per-user product. Multi-session was a
remnant of an earlier design that briefly contemplated multiple sessions
per user, but no production code path or UI ever created a second session.

This migration:

1. Picks each user's most recent session (by ``last_message_at``, falling
   back to ``created_at``) as canonical, re-keys any messages on other
   sessions to point at it, then deletes the now-empty extras. In
   practice no users have multiple sessions today, but the migration
   handles the case defensively rather than asserting.

2. Drops two columns made dead by the dead-code removal in the previous
   commit:
   - ``last_compacted_seq``: only read by the removed
     ``_consolidate_previous_session`` and ``_run_compaction_in_background``.
   - ``is_active``: defaults to True on insert and never written False
     anywhere in the codebase, so the column has no information content.

3. Adds a UNIQUE constraint on ``user_id`` so the schema enforces the
   single-conversation invariant. After this constraint is in place,
   no future code path can accidentally reintroduce multi-session.

Revision ID: 025
Revises: 024
Create Date: 2026-05-04
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "025"
down_revision: str = "024"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    bind = op.get_bind()

    # 1. Dedupe sessions: pick canonical per user, re-key messages, drop the rest.
    bind.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT
                    id,
                    user_id,
                    ROW_NUMBER() OVER (
                        PARTITION BY user_id
                        ORDER BY last_message_at DESC NULLS LAST, created_at DESC, id DESC
                    ) AS rn
                FROM sessions
            ),
            canonical AS (
                SELECT user_id, id AS canonical_id FROM ranked WHERE rn = 1
            ),
            losers AS (
                SELECT r.id AS loser_id, c.canonical_id
                FROM ranked r
                JOIN canonical c ON c.user_id = r.user_id
                WHERE r.rn > 1
            )
            UPDATE messages m
            SET session_id = l.canonical_id
            FROM losers l
            WHERE m.session_id = l.loser_id
            """
        )
    )
    bind.execute(
        sa.text(
            """
            DELETE FROM sessions
            WHERE id IN (
                SELECT id FROM (
                    SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY user_id
                        ORDER BY last_message_at DESC NULLS LAST, created_at DESC, id DESC
                    ) AS rn
                    FROM sessions
                ) ranked
                WHERE rn > 1
            )
            """
        )
    )

    # 2. Drop dead columns.
    op.drop_column("sessions", "last_compacted_seq")
    op.drop_column("sessions", "is_active")

    # 3. Enforce single-session invariant at the schema level.
    op.create_unique_constraint("uq_sessions_user_id", "sessions", ["user_id"])


def downgrade() -> None:
    op.drop_constraint("uq_sessions_user_id", "sessions", type_="unique")
    op.add_column(
        "sessions",
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "sessions",
        sa.Column("last_compacted_seq", sa.Integer(), nullable=False, server_default="0"),
    )
