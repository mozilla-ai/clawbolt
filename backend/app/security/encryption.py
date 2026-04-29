"""Envelope encryption for credential columns.

Each encrypted value carries its own random data-encryption key (DEK).
The DEK is wrapped by a key-encryption key (KEK) supplied by a
``KEKProvider``. Both the wrapped DEK and the ciphertext are stored
inline in a single column so any ``EncryptedString`` column gets
envelope encryption without schema changes.

Envelope format (text):

    clw1.<kek_id>.<base64-urlsafe(wrapped_dek)>.<fernet_token>

The ``kek_id`` lets a provider route to the correct key version on
unwrap; ``LocalKEKProvider`` uses the constant id ``local``. Premium
deployments swap in a KMS-backed provider whose ``kek_id`` is a
tenant-scoped key alias.
"""

from __future__ import annotations

import base64
import binascii
import logging
import secrets
from typing import Protocol, TypedDict

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from backend.app.config import settings

_logger = logging.getLogger(__name__)

ENVELOPE_PREFIX = "clw1"
ENVELOPE_VERSION = 1


class EncryptionContext(TypedDict, total=False):
    """Per-row context passed to wrap/unwrap.

    OSS uses only ``table`` and ``column``. Premium plugins extend with
    ``tenant_id``, ``user_id``, etc. to scope the KEK selection.
    """

    table: str
    column: str
    user_id: str
    tenant_id: str


class KEKProvider(Protocol):
    """Wraps and unwraps per-row DEKs.

    Implementations decide where the master key lives (env var, AWS KMS,
    GCP KMS, Vault Transit) and how it is selected per request. The OSS
    default is ``LocalKEKProvider``; premium plugins override via the
    auth loader.
    """

    def wrap(self, dek: bytes, *, context: EncryptionContext) -> tuple[str, bytes]:
        """Wrap *dek* with the provider's current KEK.

        Returns ``(kek_id, wrapped_dek)``. The ``kek_id`` is stored
        alongside the ciphertext so ``unwrap`` can route to the right
        key version (e.g. after a KMS key rotation).
        """
        ...

    def unwrap(self, kek_id: str, wrapped: bytes, *, context: EncryptionContext) -> bytes:
        """Unwrap a previously wrapped DEK using the key identified by *kek_id*."""
        ...


class LocalKEKProvider:
    """KEK provider backed by ``settings.encryption_key``.

    Derives a Fernet wrapping key via HKDF-SHA256. If no key is
    configured, generates an ephemeral process-local key and logs a
    warning. Ephemeral keys mean stored credentials become unreadable
    after a process restart, which is the loudest acceptable signal
    that the operator forgot to set ``ENCRYPTION_KEY``.
    """

    KEK_ID = "local"

    def __init__(self, key_material: bytes | None = None) -> None:
        if key_material is None:
            configured = settings.encryption_key.get_secret_value().encode()
            if configured:
                key_material = configured
            else:
                key_material = secrets.token_bytes(32)
                _logger.warning(
                    "ENCRYPTION_KEY not set; using ephemeral process-local KEK. "
                    "Stored credentials will become unreadable after restart."
                )
        self._fernet = Fernet(_derive_wrapping_key(key_material))

    def wrap(self, dek: bytes, *, context: EncryptionContext) -> tuple[str, bytes]:
        del context  # unused in OSS provider
        return self.KEK_ID, self._fernet.encrypt(dek)

    def unwrap(self, kek_id: str, wrapped: bytes, *, context: EncryptionContext) -> bytes:
        del context  # unused in OSS provider
        if kek_id != self.KEK_ID:
            raise ValueError(
                f"LocalKEKProvider cannot unwrap kek_id={kek_id!r}; expected {self.KEK_ID!r}"
            )
        return self._fernet.decrypt(wrapped)


def _derive_wrapping_key(key_material: bytes) -> bytes:
    """Derive a 32-byte Fernet key from raw key material via HKDF-SHA256.

    The ``info`` parameter namespaces this derivation away from the
    pre-envelope ``oauth-token-encryption`` derivation so a single
    ``ENCRYPTION_KEY`` value can't be used to read both old and new
    ciphertexts by mistake.
    """
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"clawbolt-kek-wrap-v1",
    )
    return base64.urlsafe_b64encode(hkdf.derive(key_material))


def encrypt(plaintext: str, provider: KEKProvider, context: EncryptionContext) -> str:
    """Envelope-encrypt *plaintext* and return the serialized envelope."""
    dek = Fernet.generate_key()
    kek_id, wrapped = provider.wrap(dek, context=context)
    ciphertext = Fernet(dek).encrypt(plaintext.encode()).decode()
    return _serialize(kek_id, wrapped, ciphertext)


def decrypt(envelope: str, provider: KEKProvider, context: EncryptionContext) -> str:
    """Decrypt a serialized envelope back to plaintext.

    Raises ``ValueError`` for malformed envelopes and propagates
    ``InvalidToken`` if the underlying Fernet operations fail.
    """
    kek_id, wrapped, ciphertext = _parse(envelope)
    dek = provider.unwrap(kek_id, wrapped, context=context)
    return Fernet(dek).decrypt(ciphertext.encode()).decode()


def is_envelope(value: str) -> bool:
    """Return True if *value* looks like a serialized envelope.

    Used by the migration to skip rows that have already been re-keyed
    (idempotent re-runs) and by tests.
    """
    return value.startswith(ENVELOPE_PREFIX + ".")


def _serialize(kek_id: str, wrapped: bytes, ciphertext: str) -> str:
    if "." in kek_id:
        raise ValueError(f"kek_id must not contain '.': {kek_id!r}")
    return ".".join(
        (
            ENVELOPE_PREFIX,
            kek_id,
            base64.urlsafe_b64encode(wrapped).decode(),
            ciphertext,
        )
    )


def _parse(envelope: str) -> tuple[str, bytes, str]:
    parts = envelope.split(".", 3)
    if len(parts) != 4 or parts[0] != ENVELOPE_PREFIX:
        raise ValueError(f"Malformed envelope (prefix/parts): {envelope[:32]!r}")
    _, kek_id, wrapped_b64, ciphertext = parts
    try:
        wrapped = base64.urlsafe_b64decode(wrapped_b64.encode())
    except (ValueError, binascii.Error) as exc:
        raise ValueError(f"Malformed envelope (wrapped DEK): {exc}") from exc
    return kek_id, wrapped, ciphertext


# Re-export for convenience in tests/scripts.
__all__ = [
    "ENVELOPE_PREFIX",
    "ENVELOPE_VERSION",
    "EncryptionContext",
    "InvalidToken",
    "KEKProvider",
    "LocalKEKProvider",
    "decrypt",
    "encrypt",
    "is_envelope",
]
