"""Tests for envelope encryption (KEK provider, EncryptedString, migration helper).

Covers:
- LocalKEKProvider round-trip
- A fake provider that records ``EncryptionContext`` so premium-style
  routing (per-tenant, per-user) is exercised end to end
- Malformed envelope rejection
- ``EncryptedString`` integration with the OAuthToken model
- Read of a non-envelope value raises (catches missed migrations)
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Generator
from typing import cast

import pytest

import backend.app.auth.loader as auth_loader
import backend.app.database as _db_module
from backend.app.models import OAuthToken, User
from backend.app.security.encryption import (
    ENVELOPE_PREFIX,
    EncryptionContext,
    KEKProvider,
    LocalKEKProvider,
    decrypt,
    encrypt,
    is_envelope,
)


@pytest.fixture()
def local_provider() -> LocalKEKProvider:
    return LocalKEKProvider(key_material=secrets.token_bytes(32))


class _RecordingProvider:
    """KEK provider that records every wrap/unwrap context.

    Wraps DEKs with an in-memory dict keyed by an opaque token, so the
    tests can assert the context was threaded through end to end
    without depending on any specific cryptographic backend.
    """

    KEK_ID = "recording"

    def __init__(self) -> None:
        self.wrap_calls: list[EncryptionContext] = []
        self.unwrap_calls: list[EncryptionContext] = []
        self._store: dict[bytes, bytes] = {}

    def wrap(self, dek: bytes, *, context: EncryptionContext) -> tuple[str, bytes]:
        self.wrap_calls.append(dict(context))  # type: ignore[arg-type]
        token = secrets.token_bytes(16)
        self._store[token] = dek
        return self.KEK_ID, token

    def unwrap(self, kek_id: str, wrapped: bytes, *, context: EncryptionContext) -> bytes:
        assert kek_id == self.KEK_ID
        self.unwrap_calls.append(dict(context))  # type: ignore[arg-type]
        return self._store[wrapped]


def test_local_provider_round_trip(local_provider: LocalKEKProvider) -> None:
    envelope = encrypt("hello", local_provider, {"table": "t", "column": "c"})
    assert is_envelope(envelope)
    assert envelope.startswith(ENVELOPE_PREFIX + ".")
    assert decrypt(envelope, local_provider, {"table": "t", "column": "c"}) == "hello"


def test_local_provider_rejects_unknown_kek_id(
    local_provider: LocalKEKProvider,
) -> None:
    with pytest.raises(ValueError, match="LocalKEKProvider cannot unwrap"):
        local_provider.unwrap("not-local", b"\x00" * 16, context={})


def test_recording_provider_threads_context_end_to_end() -> None:
    provider = _RecordingProvider()
    ctx: EncryptionContext = {
        "table": "oauth_tokens",
        "column": "access_token",
        "user_id": "u-1",
        "tenant_id": "tenant-abc",
    }
    envelope = encrypt("secret", cast(KEKProvider, provider), ctx)
    assert provider.wrap_calls == [ctx]
    decrypted = decrypt(envelope, cast(KEKProvider, provider), ctx)
    assert decrypted == "secret"
    assert provider.unwrap_calls == [ctx]


def test_malformed_envelope_raises(local_provider: LocalKEKProvider) -> None:
    with pytest.raises(ValueError, match="Malformed envelope"):
        decrypt("not-an-envelope", local_provider, {})
    with pytest.raises(ValueError, match="Malformed envelope"):
        decrypt("clw1.local.only-three-parts", local_provider, {})


def test_kek_id_with_dot_is_rejected(local_provider: LocalKEKProvider) -> None:
    """Serialization uses '.' as a delimiter; kek_ids must not embed it."""

    class BadProvider:
        def wrap(self, dek: bytes, *, context: EncryptionContext) -> tuple[str, bytes]:
            return "has.dot", b"x"

        def unwrap(self, kek_id: str, wrapped: bytes, *, context: EncryptionContext) -> bytes:
            return b""

    with pytest.raises(ValueError, match=r"must not contain '\.'"):
        encrypt("hello", cast(KEKProvider, BadProvider()), {})


@pytest.fixture()
def install_recording_provider() -> Generator[_RecordingProvider]:
    """Install a recording KEK provider for the duration of the test."""
    auth_loader.reset_kek_provider()
    provider = _RecordingProvider()
    auth_loader._kek_provider = cast(KEKProvider, provider)
    yield provider
    auth_loader.reset_kek_provider()


def test_encrypted_string_round_trip_through_orm(
    install_recording_provider: _RecordingProvider,
) -> None:
    """An OAuthToken row's encrypted columns round-trip through PostgreSQL."""
    db = _db_module.SessionLocal()
    try:
        user = User(id=str(uuid.uuid4()), user_id="enc-test", onboarding_complete=True)
        db.add(user)
        db.flush()

        row = OAuthToken(
            user_id=user.id,
            integration="test",
            access_token="access-plaintext",
            refresh_token="refresh-plaintext",
        )
        db.add(row)
        db.commit()
        token_id = row.id
    finally:
        db.close()

    db = _db_module.SessionLocal()
    try:
        loaded = db.get(OAuthToken, token_id)
        assert loaded is not None
        assert loaded.access_token == "access-plaintext"
        assert loaded.refresh_token == "refresh-plaintext"
    finally:
        db.close()

    contexts = install_recording_provider.wrap_calls
    columns_wrapped = sorted(c.get("column", "") for c in contexts)
    assert columns_wrapped == ["access_token", "refresh_token"]
    assert all(c.get("table") == "oauth_tokens" for c in contexts)


def test_encrypted_string_read_of_non_envelope_raises(
    install_recording_provider: _RecordingProvider,
) -> None:
    """Reading a row whose ciphertext was not migrated to envelope format
    fails fast rather than returning silently corrupted data."""
    db = _db_module.SessionLocal()
    try:
        user = User(id=str(uuid.uuid4()), user_id="enc-pre", onboarding_complete=True)
        db.add(user)
        db.flush()
        # Bypass the type decorator with a raw INSERT to simulate a row
        # left over from before migration 018.
        from sqlalchemy import text

        db.execute(
            text(
                "INSERT INTO oauth_tokens (user_id, integration, access_token, "
                "refresh_token, token_type, expires_at, scopes_json, realm_id, "
                "extra_json, created_at, updated_at) VALUES (:u, 'test', "
                "'legacy-cipher', '', 'Bearer', 0, '[]', '', '{}', NOW(), NOW())"
            ),
            {"u": user.id},
        )
        db.commit()
    finally:
        db.close()

    db = _db_module.SessionLocal()
    try:
        # SQLAlchemy materializes column values eagerly during ORM
        # loading, so the EncryptedString check fires on .one() rather
        # than on attribute access.
        with pytest.raises(RuntimeError, match="non-envelope value"):
            db.query(OAuthToken).filter(OAuthToken.integration == "test").one()
    finally:
        db.close()


def test_get_kek_provider_defaults_to_local() -> None:
    auth_loader.reset_kek_provider()
    try:
        provider = auth_loader.get_kek_provider()
        assert isinstance(provider, LocalKEKProvider)
    finally:
        auth_loader.reset_kek_provider()


def test_migration_018_rekey_helper_round_trip() -> None:
    """The migration's ``_rekey`` helper turns a pre-envelope value into
    an envelope that decrypts back to the original plaintext.

    Imports the migration via importlib because module names starting
    with a digit aren't valid Python identifiers.
    """
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "migration_018",
        Path(__file__).parent.parent
        / "alembic"
        / "versions"
        / "018_envelope_encrypt_oauth_tokens.py",
    )
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    provider = LocalKEKProvider()
    # Pre-envelope plaintext (the path taken when ENCRYPTION_KEY was unset
    # in the prior deployment). legacy=None signals "row is plaintext."
    rekeyed = migration._rekey("plain-access", None, provider, "access_token")
    assert isinstance(rekeyed, str)
    assert rekeyed.startswith(ENVELOPE_PREFIX + ".")
    assert (
        decrypt(rekeyed, provider, {"table": "oauth_tokens", "column": "access_token"})
        == "plain-access"
    )

    # Idempotent: re-running the migration leaves an envelope untouched.
    assert migration._rekey(rekeyed, None, provider, "access_token") is rekeyed

    # Empty / None rows are skipped.
    assert migration._rekey("", None, provider, "access_token") == ""
    assert migration._rekey(None, None, provider, "access_token") is None
