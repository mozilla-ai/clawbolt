"""Envelope-encrypt messages.tool_interactions_json.

Closes the privacy gap from issue #325 follow-up: ``Message.body`` and
``Message.processed_context`` were envelope-encrypted in migration 020,
and ``HeartbeatLog`` / ``MemoryDocument`` content columns followed in
022, but ``tool_interactions_json`` was left as plaintext ``Text``.

The column stores the per-turn tool call list — name, args, result,
is_error, receipt — including QuickBooks queries / customer names /
phone numbers / addresses that the LLM passes to tools and that
tools return. Anyone with direct DB access reads them verbatim, even
though every admin endpoint that surfaces them runs the strings
through ``redact_pii`` on the way out. Encrypting at rest matches the
treatment of message bodies and closes the DB-direct exposure.

The same caveats apply as 020 / 022:

* ``LocalKEKProvider()`` with no configured ENCRYPTION_KEY falls
  back to an ephemeral KEK; encrypting under that would lose the
  data on the next process restart. Refuse to run when ``messages``
  is non-empty AND ENCRYPTION_KEY is unset.
* Idempotent: rows already in envelope format (``clw1.`` prefix) are
  skipped on a re-run.
* No automatic downgrade — restore from backup if needed.

Performance: ~1ms per row on the local KEK provider; a 100k-message
DB backfills in ~2 minutes. Migration runs in a single transaction
(alembic default), so the table is locked for the duration. Operators
on million-row deployments should plan a maintenance window.

Revision ID: 024
Revises: 023
Create Date: 2026-05-03
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from backend.app.config import settings
from backend.app.security.encryption import (
    LocalKEKProvider,
    is_envelope,
)
from backend.app.security.encryption import (
    encrypt as envelope_encrypt,
)

revision: str = "024"
down_revision: str = "023"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


_BATCH_SIZE = 1000


def upgrade() -> None:
    bind = op.get_bind()

    # Same preflight as 020 / 022. An empty messages table is fine
    # (nothing to lose); a populated one without ENCRYPTION_KEY would
    # silently rotate everything under an ephemeral key.
    if not settings.encryption_key.get_secret_value():
        first_row = bind.execute(sa.text("SELECT 1 FROM messages LIMIT 1")).first()
        if first_row is not None:
            raise RuntimeError(
                "Migration 024 requires ENCRYPTION_KEY to be set when the "
                "messages table is non-empty. Without a stable key, the "
                "envelope-encrypted tool_interactions_json blobs would "
                "become unrecoverable after the next process restart. "
                "Set ENCRYPTION_KEY (any random 32-byte value) and "
                "re-run the migration."
            )

    provider = LocalKEKProvider()

    last_id = 0
    while True:
        rows = bind.execute(
            sa.text(
                "SELECT id, tool_interactions_json FROM messages "
                "WHERE id > :last ORDER BY id LIMIT :limit"
            ),
            {"last": last_id, "limit": _BATCH_SIZE},
        ).fetchall()
        if not rows:
            break
        for row_id, value in rows:
            new_value = _rekey(value, provider)
            if new_value is value:
                continue
            bind.execute(
                sa.text("UPDATE messages SET tool_interactions_json = :v WHERE id = :id"),
                {"v": new_value, "id": row_id},
            )
            last_id = row_id
        # End-of-batch cursor advance so a fully-migrated DB on re-run
        # does not loop forever on the same SELECT.
        last_id = max(last_id, rows[-1][0])


def _rekey(value: str | None, provider: LocalKEKProvider) -> str | None:
    """Re-encrypt one tool_interactions_json value under the envelope format.

    Idempotent: rows already in envelope format are returned unchanged
    (identity) so the caller can detect "nothing to do" via ``is`` and
    skip the UPDATE. Empty strings and NULLs pass through untouched.
    """
    if not value:
        return value
    if is_envelope(value):
        return value
    return envelope_encrypt(
        value,
        provider,
        {"table": "messages", "column": "tool_interactions_json"},
    )


def downgrade() -> None:
    """No automatic downgrade.

    Decrypting back to plaintext would require all envelopes to unwrap
    against the configured KEK, which is a snapshot operation that
    can't be re-done if the KEK rotates. Operators who need to roll
    back must restore from backup.
    """
    raise NotImplementedError("Cannot downgrade revision 024; restore from backup if needed.")
