"""Add envelope-encrypted ``llm_reply_text`` column to messages.

Captures the LLM's pre-receipt prose for outbound messages. Today,
``messages.body`` stores the dispatched body (LLM prose plus the
deterministic receipt block appended by ``append_receipts``). The same
column is then read back when reconstructing conversation history for the
LLM, which trains the model on its own past output including the receipt
block. The model treats that as the canonical reply shape and reproduces
the receipt bullet on its next turn, forcing a post-hoc grep dedup to
clean up. ``llm_reply_text`` captures the LLM's prose *before* the
receipt block is appended so the rebuild path can feed the LLM its own
text without the feedback loop.

Empty for inbound rows and for outbound rows persisted before this
migration (``load_conversation_history`` falls back to ``body`` when the
column is empty, preserving behavior for legacy rows).

Encrypted at rest under ``EncryptedString`` like the rest of the
user-authored content on this table: the LLM reply quotes user content
back and would expose the same PII as ``body`` if left in plaintext.

NOT NULL with a server default of empty string so existing rows
backfill cleanly and raw-SQL inserts in older migration tests (which
omit the column) keep working.

Revision ID: 037
Revises: 036
Create Date: 2026-05-19
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "037"
down_revision: str = "036"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("llm_reply_text", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("messages", "llm_reply_text")
