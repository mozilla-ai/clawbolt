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


class EncryptionError(Exception):
    """Raised when an envelope cannot be parsed or decrypted.

    Wraps the lower-level failure modes (malformed structure, bad
    base64, ``InvalidToken`` from Fernet, ``UnicodeDecodeError`` on the
    plaintext) under a single public type so callers can catch one
    exception and decide whether to degrade gracefully or surface to
    the user. The original cause is preserved via ``__cause__`` (i.e.
    ``raise EncryptionError(...) from exc``) so logs and tracebacks
    keep enough detail to diagnose corruption.

    Issue #1223: previously corrupt envelopes raised ``ValueError`` for
    structural problems and ``InvalidToken`` for cryptographic ones,
    making it tempting for callers to ``except Exception`` and silently
    return empty/partial data. The dedicated type makes the contract
    explicit.
    """


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

    Raises ``EncryptionError`` for any corruption mode: malformed
    envelope structure, a wrapped DEK that fails to unwrap, a
    ciphertext that fails Fernet authentication, or plaintext that
    isn't valid UTF-8. The original exception is chained via
    ``__cause__`` so callers that log the failure get the underlying
    detail without having to enumerate cryptographic exception types.

    Fail-loud is intentional (issue #1223). Earlier code paths that
    caught ``Exception`` and returned ``""`` turned a corrupted row
    into silently-empty data downstream; the multi-append memory bug
    in #1200 was the visible symptom.
    """
    kek_id, wrapped, ciphertext = _parse(envelope)
    try:
        dek = provider.unwrap(kek_id, wrapped, context=context)
        return Fernet(dek).decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise EncryptionError(
            f"Envelope failed authenticated decryption (kek_id={kek_id!r}): {exc}"
        ) from exc
    except UnicodeDecodeError as exc:
        raise EncryptionError(
            f"Decrypted plaintext is not valid UTF-8 (kek_id={kek_id!r}): {exc}"
        ) from exc


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
    """Split a serialized envelope into ``(kek_id, wrapped_dek, ciphertext)``.

    Raises ``EncryptionError`` (not ``ValueError``) on any structural
    problem. Issue #1223: callers must not silently treat corrupt
    envelopes as empty/partial data, so this surfaces a dedicated
    exception type that's easy to catch deliberately and impossible to
    catch by accident under a broad ``except ValueError``.
    """
    parts = envelope.split(".", 3)
    if len(parts) != 4 or parts[0] != ENVELOPE_PREFIX:
        raise EncryptionError(f"Malformed envelope (prefix/parts): {envelope[:32]!r}")
    _, kek_id, wrapped_b64, ciphertext = parts
    try:
        wrapped = base64.urlsafe_b64decode(wrapped_b64.encode())
    except (ValueError, binascii.Error) as exc:
        raise EncryptionError(f"Malformed envelope (wrapped DEK): {exc}") from exc
    return kek_id, wrapped, ciphertext


# Re-export for convenience in tests/scripts.
__all__ = [
    "ENVELOPE_PREFIX",
    "EncryptionContext",
    "EncryptionError",
    "InvalidToken",
    "KEKProvider",
    "LocalKEKProvider",
    "decrypt",
    "encrypt",
    "is_envelope",
]
