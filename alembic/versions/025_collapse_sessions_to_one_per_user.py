"""Collapse the sessions table to one row per user.

Clawbolt is a single-conversation-per-user product. Multi-session was a
remnant of an earlier design that briefly contemplated multiple sessions
per user, but no production code path or UI ever created a second session.

This migration:

1. Picks each user's *oldest* session (by ``created_at``, falling back
   to ``last_message_at`` and ``id``) as canonical, renumbers any
   messages on other ("loser") sessions to follow the canonical's
   ``MAX(seq)``, re-keys those messages onto the canonical, then
   deletes the now-empty losers. In practice no users have multiple
   sessions today, but the migration preserves data defensively rather
   than asserting.

   Two design notes:
   - The ``messages`` table has ``UNIQUE(session_id, seq)``, and every
     session starts at ``seq=1``. A naive UPDATE that only re-keyed
     ``session_id`` would collide on first overlap. The renumber step
     bumps loser seqs above the canonical's ``MAX(seq)`` so the final
     state is unique.
   - Picking the oldest session as canonical (rather than the newest)
     keeps the merged seq ordering chronological: canonical messages
     are oldest with low seqs, loser messages are newer with higher
     seqs. The ``session_id`` of the surviving row is no longer
     load-bearing for the frontend, so this choice is safe.

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

    # 1. Dedupe sessions. For each user, pick a canonical session (the
    #    oldest), renumber loser-session messages to follow the
    #    canonical's MAX(seq), re-key them onto the canonical, then
    #    delete the now-empty loser rows. Every step is idempotent if
    #    the user only had one session to begin with (the no-op case
    #    that applies to ~all production users).
    #
    #    The two ORDER BY clauses below MUST agree on which session is
    #    canonical (rn=1) and which are losers (rn>1); otherwise the
    #    DELETE would drop the session whose messages we just rekeyed.
    bind.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT
                    id,
                    user_id,
                    ROW_NUMBER() OVER (
                        PARTITION BY user_id
                        ORDER BY created_at ASC, last_message_at ASC NULLS FIRST, id ASC
                    ) AS rn
                FROM sessions
            ),
            canonical AS (
                SELECT user_id, id AS canonical_id FROM ranked WHERE rn = 1
            ),
            canonical_max_seq AS (
                SELECT
                    c.canonical_id,
                    COALESCE(MAX(m.seq), 0) AS max_seq
                FROM canonical c
                LEFT JOIN messages m ON m.session_id = c.canonical_id
                GROUP BY c.canonical_id
            ),
            losers AS (
                SELECT
                    r.id AS loser_id,
                    cms.canonical_id,
                    cms.max_seq
                FROM ranked r
                JOIN canonical c ON c.user_id = r.user_id
                JOIN canonical_max_seq cms ON cms.canonical_id = c.canonical_id
                WHERE r.rn > 1
            ),
            renumbered AS (
                SELECT
                    m.id AS message_id,
                    l.canonical_id,
                    l.max_seq + ROW_NUMBER() OVER (
                        PARTITION BY l.canonical_id
                        ORDER BY m.timestamp, m.id
                    ) AS new_seq
                FROM messages m
                JOIN losers l ON m.session_id = l.loser_id
            )
            UPDATE messages m
            SET session_id = r.canonical_id, seq = r.new_seq
            FROM renumbered r
            WHERE m.id = r.message_id
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
                        ORDER BY created_at ASC, last_message_at ASC NULLS FIRST, id ASC
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
