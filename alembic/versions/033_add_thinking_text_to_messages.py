"""Add envelope-encrypted ``thinking_text`` column to messages.

Captures the LLM's extended-thinking output (Anthropic ``thinking`` blocks)
that ``MessageResponse`` returns alongside the final assistant text. Today
the agent loop discards these blocks via ``get_response_text`` after the
last LLM call lands; with this column populated, the assistant message row
carries the reasoning that produced its ``body`` so admins can audit "why
did the agent reply this way" without re-querying the LLM.

Encrypted at rest under ``EncryptedString`` like ``body`` /
``processed_context`` / ``tool_interactions_json``: the thinking stream
quotes back user-supplied content (names, addresses, integration payloads)
and would expose the same PII as the message body if left in plaintext.

Nullable / default empty so existing rows remain readable; outbound
messages persisted before this migration ran simply have no captured
thinking. Inbound messages always have an empty value (the column is only
written by the agent's outbound persistence path).

Revision ID: 033
Revises: 032
Create Date: 2026-05-07
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "033"
down_revision: str = "032"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("thinking_text", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("messages", "thinking_text")
