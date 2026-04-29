"""Re-key oauth_tokens.access_token / refresh_token to envelope format.

Each existing ciphertext was produced by a single HKDF-derived Fernet
key (``info=b"oauth-token-encryption"``). Each row is decrypted with
that key (or read as-is if the deployment had ``ENCRYPTION_KEY`` empty
and stored plaintext) and re-encrypted under a per-row DEK whose wrap
is delegated to ``LocalKEKProvider``. The new envelope is self-
identifying via the ``clw1.`` prefix, so this migration is idempotent:
already-envelope rows are skipped on re-run.

Deployment ordering note: run this migration *before* the new
application code rolls out. The new ``EncryptedString`` raises on
non-envelope reads, so a code-rolled-out / migration-not-run gap will
fail health checks. For premium (``clawbolt-premium``), this means
``uv run alembic -c alembic.ini upgrade head`` against the production
database during deploy, before swapping container images.

Revision ID: 018
Revises: 017
Create Date: 2026-04-29
"""

from __future__ import annotations

import base64

import sqlalchemy as sa
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from alembic import op
from backend.app.config import settings
from backend.app.security.encryption import (
    LocalKEKProvider,
    is_envelope,
)
from backend.app.security.encryption import (
    encrypt as envelope_encrypt,
)

revision: str = "018"
down_revision: str = "017"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def _legacy_fernet() -> Fernet | None:
    """Reproduce the pre-envelope HKDF/Fernet derivation.

    Returns None when no ``ENCRYPTION_KEY`` is configured, meaning rows
    were stored as plaintext.
    """
    key = settings.encryption_key.get_secret_value()
    if not key:
        return None
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"oauth-token-encryption",
    )
    derived = hkdf.derive(key.encode())
    return Fernet(base64.urlsafe_b64encode(derived))


def _decrypt_legacy(value: str, fernet: Fernet | None) -> str:
    """Decrypt a pre-envelope row.

    If a legacy key is configured and decryption succeeds, returns the
    plaintext. If decryption fails (or no key is set), the row was
    stored as plaintext under the old "no-key" path; return it as-is.
    """
    if fernet is None:
        return value
    try:
        return fernet.decrypt(value.encode()).decode()
    except InvalidToken:
        return value


def upgrade() -> None:
    bind = op.get_bind()
    legacy = _legacy_fernet()
    provider = LocalKEKProvider()

    rows = bind.execute(
        sa.text("SELECT id, access_token, refresh_token FROM oauth_tokens")
    ).fetchall()

    for row_id, access_token, refresh_token in rows:
        new_access = _rekey(access_token, legacy, provider, "access_token")
        new_refresh = _rekey(refresh_token, legacy, provider, "refresh_token")
        if new_access is access_token and new_refresh is refresh_token:
            continue
        bind.execute(
            sa.text("UPDATE oauth_tokens SET access_token = :a, refresh_token = :r WHERE id = :id"),
            {"a": new_access, "r": new_refresh, "id": row_id},
        )


def _rekey(
    value: str | None,
    legacy: Fernet | None,
    provider: LocalKEKProvider,
    column: str,
) -> str | None:
    if not value:
        return value
    if is_envelope(value):
        # Already migrated (idempotent re-run).
        return value
    plaintext = _decrypt_legacy(value, legacy)
    return envelope_encrypt(
        plaintext,
        provider,
        {"table": "oauth_tokens", "column": column},
    )


def downgrade() -> None:
    """No automatic downgrade.

    Recovering pre-envelope ciphertext would require the legacy HKDF
    key to be configured and would still re-encrypt under the old
    single-key scheme, which we explicitly removed. Operators who need
    to roll back must restore from backup.
    """
    raise NotImplementedError("Cannot downgrade revision 018; restore from backup if needed.")
