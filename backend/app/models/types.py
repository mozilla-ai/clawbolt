"""Column types shared by the models, notably envelope-encrypted strings."""

from __future__ import annotations

from sqlalchemy import (
    Text,
)
from sqlalchemy.types import TypeDecorator

from backend.app.security import encryption as _encryption


class EncryptedString(TypeDecorator):
    """Envelope-encrypts string values at rest.

    On write, generates a fresh per-row data-encryption key (DEK),
    Fernet-encrypts the plaintext with it, and asks the configured
    ``KEKProvider`` to wrap the DEK. The wrapped DEK and the ciphertext
    are serialized into a single inline envelope (see
    ``backend.app.security.encryption``).

    On read, the envelope is parsed, the DEK is unwrapped via the
    provider, and the plaintext is recovered.

    The provider is selected through ``backend.app.auth.loader``:
    ``LocalKEKProvider`` by default (Fernet wrapping derived from
    ``settings.encryption_key``), or a KMS-backed provider when
    ``KMS_KEY_ARN`` is set.
    """

    impl = Text
    cache_ok = True

    def __init__(self, *, table: str = "", column: str = "") -> None:
        super().__init__()
        self._table = table
        self._column = column

    def _context(self) -> _encryption.EncryptionContext:
        ctx: _encryption.EncryptionContext = {}
        if self._table:
            ctx["table"] = self._table
        if self._column:
            ctx["column"] = self._column
        return ctx

    def _get_provider(self) -> _encryption.KEKProvider:
        # Imported lazily to avoid an import cycle: loader imports models
        # transitively via the auth backend stack.
        from backend.app.auth.loader import get_kek_provider

        return get_kek_provider()

    def process_bind_param(self, value, dialect):  # noqa: ANN001, ANN201
        if value is None or value == "":
            return value
        provider = self._get_provider()
        return _encryption.encrypt(value, provider, self._context())

    def process_result_value(self, value, dialect):  # noqa: ANN001, ANN201
        if value is None or value == "":
            return value
        if not _encryption.is_envelope(value):
            # Migration 018 re-keys every existing row to the envelope
            # format. A non-envelope value here means the migration
            # didn't run (or a legacy code path bypassed the type
            # decorator). Fail loudly rather than silently return
            # ciphertext or plaintext that would corrupt downstream use.
            raise RuntimeError(
                "EncryptedString read found a non-envelope value. Run "
                "`uv run alembic upgrade head` to re-key existing rows."
            )
        provider = self._get_provider()
        return _encryption.decrypt(value, provider, self._context())
