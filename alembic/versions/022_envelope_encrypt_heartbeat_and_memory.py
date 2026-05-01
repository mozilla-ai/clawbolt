"""Envelope-encrypt heartbeat_logs + memory_documents content columns.

Extends the at-rest encryption coverage from migration 020 (Message
body / processed_context). Same pattern: per-row DEKs wrapped by
``LocalKEKProvider`` (or the premium KMS-backed provider). Five
columns get re-encrypted in this migration:

- ``heartbeat_logs.message_text`` (proactive message body)
- ``heartbeat_logs.reasoning`` (LLM rationale, often paraphrases user)
- ``heartbeat_logs.tasks`` (serialized task state with user-authored
  task descriptions)
- ``memory_documents.memory_text`` (working memory file)
- ``memory_documents.history_text`` (compacted older sessions)

Operator preflight: refuses to run on non-empty target tables when
``ENCRYPTION_KEY`` is unset, matching the migration-020 contract.
Empty tables pass through (nothing to lose).

Idempotent: rows already in envelope format are returned by identity
from ``_rekey`` and the loop skips the UPDATE.

Performance: streams in 1000-row batches with ``WHERE id > :last``
cursor advancement (heartbeat_logs can grow large on chatty
deployments). Memory_documents has at most one row per user, so the
batching there is overkill but keeps the code uniform.

Revision ID: 022
Revises: 021
Create Date: 2026-05-01
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

revision: str = "022"
down_revision: str = "021"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


_BATCH_SIZE = 1000


# Each entry: (table, [columns to encrypt]). Keep this list aligned
# with the ``EncryptedString(table=..., column=...)`` declarations in
# ``backend/app/models.py`` so the on-disk envelope context matches
# what ``EncryptedString.process_result_value`` will pass on read.
_TARGETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("heartbeat_logs", ("message_text", "reasoning", "tasks")),
    ("memory_documents", ("memory_text", "history_text")),
)


def upgrade() -> None:
    bind = op.get_bind()

    # Refuse to run on non-empty target tables when ENCRYPTION_KEY is
    # unset. ``LocalKEKProvider()`` with no configured key falls back
    # to an ephemeral process-local KEK; encrypting under it would
    # leave content unrecoverable after the next process restart. An
    # empty table is fine: nothing to lose.
    if not settings.encryption_key.get_secret_value():
        for table, _cols in _TARGETS:
            first_row = bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first()
            if first_row is not None:
                raise RuntimeError(
                    f"Migration 022 requires ENCRYPTION_KEY to be set when "
                    f"the {table} table is non-empty. Without a stable key, "
                    f"content would become unrecoverable after the next "
                    f"process restart. Set ENCRYPTION_KEY (any random 32-byte "
                    f"value) and re-run the migration."
                )

    provider = LocalKEKProvider()

    for table, cols in _TARGETS:
        _backfill_table(bind, provider, table, cols)


def _backfill_table(
    bind: sa.Connection,
    provider: LocalKEKProvider,
    table: str,
    cols: tuple[str, ...],
) -> None:
    """Stream the rows of *table* in batches, re-encrypting *cols*.

    The cursor advancement at the end of every batch (regardless of
    whether anything was updated) handles the all-already-encrypted
    case on a re-run. Without it, a re-run on a fully-migrated DB
    would loop forever on the same SELECT.
    """
    select_cols = ", ".join(["id", *cols])
    set_clause = ", ".join(f"{c} = :{c}" for c in cols)
    update_sql = sa.text(f"UPDATE {table} SET {set_clause} WHERE id = :id")
    select_sql = sa.text(
        f"SELECT {select_cols} FROM {table} WHERE id > :last ORDER BY id LIMIT :limit"
    )

    last_id = 0
    while True:
        rows = bind.execute(select_sql, {"last": last_id, "limit": _BATCH_SIZE}).fetchall()
        if not rows:
            break
        for row in rows:
            row_id = row[0]
            old_values = {col: row[i + 1] for i, col in enumerate(cols)}
            new_values = {col: _rekey(old_values[col], provider, table, col) for col in cols}
            if all(new_values[c] is old_values[c] for c in cols):
                continue
            params: dict[str, object] = {"id": row_id, **new_values}
            bind.execute(update_sql, params)
            last_id = row_id
        last_id = max(last_id, rows[-1][0])


def _rekey(value: str | None, provider: LocalKEKProvider, table: str, column: str) -> str | None:
    """Re-encrypt one column value under the envelope format.

    Idempotent: rows already in envelope format are returned unchanged
    by identity so the caller can detect "nothing to do" and skip the
    UPDATE. Empty strings and NULLs pass through untouched (matches
    ``EncryptedString.process_bind_param`` on writes).
    """
    if not value:
        return value
    if is_envelope(value):
        return value
    return envelope_encrypt(value, provider, {"table": table, "column": column})


def downgrade() -> None:
    """No automatic downgrade.

    Decrypting back to plaintext would require unwrapping every
    envelope at a moment in time, which fails if the KEK rotates
    between upgrade and downgrade. Restore from backup is the
    documented recovery path.
    """
    raise NotImplementedError("Cannot downgrade revision 022; restore from backup if needed.")
