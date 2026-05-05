"""Capture the compaction LLM call (prompt, raw response, parsed result).

Layer 5 follow-up to migration 030. Migration 030 added before/after
snapshots of the four memory files this event touched, which answers
"what changed?" but not "why did the LLM choose to update only some
files?" These three columns close that gap so an admin reviewing a
compaction event can see the trimmed conversation that was passed to
the LLM, the raw text it returned (useful when JSON parsing falls back
to the empty result), and the parsed-fields-as-JSON structure that the
``*_updated`` flags + ``summary_len`` only summarize.

- ``prompt_text``: the trimmed conversation text fed to the compaction
  LLM as the ``<conversation>`` block. Excludes the static system
  prompt and the four current memory-file inputs (those are already
  captured by the ``*_text_before`` snapshots from migration 030 and
  the static system prompt is identical across events).
- ``raw_response_text``: ``get_response_text(response)`` before
  ``_parse_compaction_response`` runs. Catches malformed JSON, prompt
  drift, and the markdown-fence path that today produces a silent empty
  result.
- ``parsed_response_json``: a JSON string of the four parsed fields
  (memory_update / summary / user_profile_update / soul_update). Lets
  admins see the exact strings the LLM produced rather than only the
  derived bool flags.

All three columns are nullable so existing rows and pending rows
remain readable without a backfill. Same envelope-encrypted text shape
as the 030 snapshot columns.

Revision ID: 031
Revises: 030
Create Date: 2026-05-05
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "031"
down_revision: str = "030"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    for col in ("prompt_text", "raw_response_text", "parsed_response_json"):
        op.add_column(
            "compaction_events",
            sa.Column(col, sa.Text(), nullable=True),
        )


def downgrade() -> None:
    for col in ("parsed_response_json", "raw_response_text", "prompt_text"):
        op.drop_column("compaction_events", col)
