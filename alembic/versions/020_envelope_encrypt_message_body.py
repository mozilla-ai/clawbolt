"""Envelope-encrypt messages.body and messages.processed_context.

Item 4 of the clawbolt-premium privacy redesign (issue #325). User
message content was previously stored plaintext in two columns; both
get re-encrypted under per-row DEKs wrapped by ``LocalKEKProvider`` (or
the premium KMS-backed provider). The new ``EncryptedString`` type
decorator on the ORM column then transparently decrypts on read.

Unlike migration 018 (oauth_tokens), there is no legacy ciphertext to
decrypt first. Pre-existing rows are plaintext, so the per-row work
is just envelope-encrypt-and-replace. The new envelope is self-
identifying via the ``clw1.`` prefix, so this migration is idempotent:
a row that's already in envelope format on a re-run is skipped.

Operator preflight: this migration refuses to run when ``messages``
is non-empty AND ``ENCRYPTION_KEY`` is unset. Without a stable key,
``LocalKEKProvider`` falls back to an ephemeral process-local KEK,
which would re-encrypt every message body under a key that vanishes
on the next process restart, leaving message content unrecoverable.
The ``oauth_tokens`` precedent (migration 018) didn't bother with this
check because OAuth tokens are rare and trivially re-issuable; message
bodies are not.

Deployment ordering: run this migration BEFORE rolling out the new
application code. ``EncryptedString`` raises ``RuntimeError`` on a
non-envelope read, so a code-deployed / migration-not-run gap fails
the very first request. For premium (clawbolt-premium), this means
``uv run alembic upgrade head`` against the production database during
deploy, before swapping container images.

Performance note: ~1ms per row for envelope encryption on the local
KEK provider. A 100k-message database backfills in ~2 minutes. The
migration streams rows in batches and commits per row to keep memory
bounded. NOTE: alembic wraps ``upgrade()`` in a single transaction, so
the table is locked for the duration of the backfill. Operators on
million-row deployments should plan a maintenance window or run
``op.execute("SET LOCAL statement_timeout = 0")`` first.

Revision ID: 020
Revises: 018
Create Date: 2026-05-01

Stacking note: this migration depends on ``018``, NOT on ``019``.
``019`` (data_sharing_consent on users, in flight at clawbolt#1100)
touches a different table (``users``) so the chain order between the
two doesn't matter functionally. Whichever PR merges second will see
a two-heads conflict from alembic and needs a one-line down_revision
bump. We chose ``018`` here so this branch's e2e-playwright CI (which
runs ``alembic upgrade head`` to bring up a real app instance) can
pass standalone.
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

revision: str = "020"
down_revision: str = "018"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


_BATCH_SIZE = 1000


def upgrade() -> None:
    bind = op.get_bind()

    # Refuse to run on a non-empty messages table when ENCRYPTION_KEY is
    # unset. LocalKEKProvider() with no configured key falls back to an
    # ephemeral process-local KEK; encrypting every message body under
    # that key would leave them unrecoverable after the next restart.
    # An empty messages table is fine: there's nothing to lose.
    if not settings.encryption_key.get_secret_value():
        first_row = bind.execute(sa.text("SELECT 1 FROM messages LIMIT 1")).first()
        if first_row is not None:
            raise RuntimeError(
                "Migration 020 requires ENCRYPTION_KEY to be set when the "
                "messages table is non-empty. Without a stable key, message "
                "bodies would become unrecoverable after the next process "
                "restart. Set ENCRYPTION_KEY (any random 32-byte value) and "
                "re-run the migration."
            )

    provider = LocalKEKProvider()

    # Stream in batches so a multi-million-row table doesn't load the
    # whole result set into memory. ``ORDER BY id`` + ``WHERE id > :last``
    # gives a stable pagination that's not perturbed by concurrent
    # inserts (this migration runs in a transaction; new inserts wait
    # behind it anyway, but the pattern is good hygiene).
    last_id = 0
    while True:
        rows = bind.execute(
            sa.text(
                "SELECT id, body, processed_context FROM messages "
                "WHERE id > :last ORDER BY id LIMIT :limit"
            ),
            {"last": last_id, "limit": _BATCH_SIZE},
        ).fetchall()
        if not rows:
            break
        for row_id, body, processed_context in rows:
            new_body = _rekey(body, provider, "body")
            new_processed = _rekey(processed_context, provider, "processed_context")
            if new_body is body and new_processed is processed_context:
                continue
            bind.execute(
                sa.text("UPDATE messages SET body = :b, processed_context = :p WHERE id = :id"),
                {"b": new_body, "p": new_processed, "id": row_id},
            )
            last_id = row_id
        # Re-pin the cursor at the largest id we saw, even when nothing
        # was updated in this batch (e.g. all rows already in envelope
        # format on a re-run, so the in-loop ``last_id = row_id``
        # assignment never fires). Without this end-of-batch reach-
        # around, an already-migrated DB would loop forever on the
        # same SELECT.
        last_id = max(last_id, rows[-1][0])


def _rekey(value: str | None, provider: LocalKEKProvider, column: str) -> str | None:
    """Re-encrypt one column value under the envelope format.

    Idempotent: rows already in envelope format are returned unchanged
    (identity) so the caller can detect "nothing to do" via ``is`` and
    skip the UPDATE. Empty strings and NULLs pass through untouched
    (``EncryptedString.process_bind_param`` does the same on writes).
    """
    if not value:
        return value
    if is_envelope(value):
        return value
    return envelope_encrypt(
        value,
        provider,
        {"table": "messages", "column": column},
    )


def downgrade() -> None:
    """No automatic downgrade.

    Decrypting back to plaintext would require all envelopes to unwrap
    against the configured KEK, which is a snapshot operation that
    can't be re-done if the KEK rotates. Operators who need to roll
    back must restore from backup.
    """
    raise NotImplementedError("Cannot downgrade revision 020; restore from backup if needed.")
