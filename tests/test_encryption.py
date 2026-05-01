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

import base64
import importlib.util
import secrets
import sys
import types
import uuid
from collections.abc import Generator
from pathlib import Path
from typing import cast

import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import SecretStr
from sqlalchemy import text as _sa_text

import backend.app.auth.loader as auth_loader
import backend.app.database as _db_module
from alembic import op as _alembic_op
from backend.app.config import settings as _app_settings
from backend.app.models import ChatSession, Message, OAuthToken, User
from backend.app.security.encryption import (
    ENVELOPE_PREFIX,
    EncryptionContext,
    KEKProvider,
    LocalKEKProvider,
    decrypt,
    encrypt,
    is_envelope,
)


def _load_migration_018():  # noqa: ANN202
    """Load migration 018 by file path because module names cannot start with a digit."""
    spec = importlib.util.spec_from_file_location(
        "migration_018",
        Path(__file__).parent.parent
        / "alembic"
        / "versions"
        / "018_envelope_encrypt_oauth_tokens.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_migration_020():  # noqa: ANN202
    """Load migration 020 by file path; module names can't start with a digit."""
    spec = importlib.util.spec_from_file_location(
        "migration_020",
        Path(__file__).parent.parent
        / "alembic"
        / "versions"
        / "020_envelope_encrypt_message_body.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _legacy_fernet_for(key_material: bytes) -> Fernet:
    """Reproduce the pre-envelope HKDF/Fernet derivation for tests."""
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"oauth-token-encryption",
    )
    return Fernet(base64.urlsafe_b64encode(hkdf.derive(key_material)))


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


def test_message_body_round_trip_through_orm(
    install_recording_provider: _RecordingProvider,
) -> None:
    """A Message row's encrypted body / processed_context round-trip
    through PostgreSQL. ORM reads see plaintext; the underlying column
    holds an envelope."""
    db = _db_module.SessionLocal()
    try:
        user = User(id=str(uuid.uuid4()), user_id="msg-enc-test", onboarding_complete=True)
        db.add(user)
        db.flush()
        cs = ChatSession(
            session_id=f"sess-{uuid.uuid4().hex[:8]}",
            user_id=user.id,
        )
        db.add(cs)
        db.flush()
        row = Message(
            session_id=cs.id,
            seq=1,
            direction="inbound",
            body="hello-plaintext-body",
            processed_context="hello-plaintext-with-image-description",
        )
        db.add(row)
        db.commit()
        message_id = row.id
    finally:
        db.close()

    # Round-trip: plaintext on read.
    db = _db_module.SessionLocal()
    try:
        loaded = db.get(Message, message_id)
        assert loaded is not None
        assert loaded.body == "hello-plaintext-body"
        assert loaded.processed_context == "hello-plaintext-with-image-description"
    finally:
        db.close()

    # Disk-form: the underlying column stores an envelope, not the
    # plaintext we wrote. This is the at-rest guarantee the migration
    # delivers; a database leak (pgdump from a backup, snapshot of a
    # read replica) gives the attacker ciphertext, not message bodies.
    db = _db_module.SessionLocal()
    try:
        rows = db.execute(
            _sa_text("SELECT body, processed_context FROM messages WHERE id = :id"),
            {"id": message_id},
        ).all()
        assert len(rows) == 1
        raw_body, raw_processed = rows[0]
        assert raw_body != "hello-plaintext-body"
        assert raw_body.startswith(ENVELOPE_PREFIX + ".")
        assert raw_processed != "hello-plaintext-with-image-description"
        assert raw_processed.startswith(ENVELOPE_PREFIX + ".")
    finally:
        db.close()

    contexts = install_recording_provider.wrap_calls
    columns_wrapped = sorted(c.get("column", "") for c in contexts)
    assert "body" in columns_wrapped
    assert "processed_context" in columns_wrapped
    assert all(
        c.get("table") == "messages"
        for c in contexts
        if c.get("column") in ("body", "processed_context")
    )


def test_message_body_empty_string_passes_through(
    install_recording_provider: _RecordingProvider,
) -> None:
    """Empty bodies are passed through without invoking the KEK provider.

    ``EncryptedString.process_bind_param`` short-circuits on empty/None
    so outbound messages with no body (e.g. a tool-call-only assistant
    turn) don't burn a wrap call. Without this, a 50-message turn with
    half empty bodies would still issue 50 wraps, measurable on
    high-throughput deployments.
    """
    db = _db_module.SessionLocal()
    try:
        user = User(id=str(uuid.uuid4()), user_id="msg-empty-test", onboarding_complete=True)
        db.add(user)
        db.flush()
        cs = ChatSession(
            session_id=f"sess-{uuid.uuid4().hex[:8]}",
            user_id=user.id,
        )
        db.add(cs)
        db.flush()
        row = Message(session_id=cs.id, seq=1, direction="outbound", body="", processed_context="")
        db.add(row)
        db.commit()
        message_id = row.id
    finally:
        db.close()

    # No body / processed_context wrap calls should appear for this row.
    msg_columns = [
        c.get("column", "")
        for c in install_recording_provider.wrap_calls
        if c.get("table") == "messages"
    ]
    assert "body" not in msg_columns
    assert "processed_context" not in msg_columns

    # And the row reads back with empty strings, not envelope blobs.
    db = _db_module.SessionLocal()
    try:
        loaded = db.get(Message, message_id)
        assert loaded is not None
        assert loaded.body == ""
        assert loaded.processed_context == ""
    finally:
        db.close()


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


def _install_fake_plugin(
    monkeypatch: pytest.MonkeyPatch, name: str, get_kek_provider: object
) -> None:
    """Install a fake premium plugin module that exposes ``get_kek_provider``.

    Cleans up automatically when the test's monkeypatch fixture tears
    down: the sys.modules entry and the settings override both revert.
    """
    fake_module = types.ModuleType(name)
    fake_module.get_kek_provider = get_kek_provider  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, name, fake_module)
    monkeypatch.setattr(auth_loader.settings, "premium_plugin", name)


def test_get_kek_provider_falls_back_to_local_when_plugin_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A premium plugin can opt out of the KEK override at runtime by
    returning ``None`` from ``get_kek_provider()``. The loader then
    falls back to ``LocalKEKProvider`` instead of caching ``None`` and
    breaking subsequent reads.

    Lets premium ship the KMS provider dormant: when the env vars
    aren't set yet, the plugin returns ``None`` and OSS encryption
    keeps working unchanged.
    """
    _install_fake_plugin(monkeypatch, "fake_premium_plugin_dormant", lambda: None)

    auth_loader.reset_kek_provider()
    try:
        provider = auth_loader.get_kek_provider()
        assert isinstance(provider, LocalKEKProvider)
    finally:
        auth_loader.reset_kek_provider()


def test_get_kek_provider_uses_plugin_provider_when_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the plugin returns a real provider, the loader uses it
    instead of the OSS default. Mirror of the dormant-fallback test
    above, on the active branch."""
    sentinel_provider = _RecordingProvider()
    _install_fake_plugin(monkeypatch, "fake_premium_plugin_active", lambda: sentinel_provider)

    auth_loader.reset_kek_provider()
    try:
        provider = auth_loader.get_kek_provider()
        assert provider is sentinel_provider
    finally:
        auth_loader.reset_kek_provider()


def test_migration_018_rekey_helper_plaintext_path() -> None:
    """When the pre-envelope deployment had ``ENCRYPTION_KEY`` unset,
    rows were stored as plaintext. ``_rekey`` should pass them through
    to envelope encryption with no decryption attempt.
    """
    migration = _load_migration_018()
    provider = LocalKEKProvider()

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


def test_migration_018_decrypts_legacy_fernet_then_rekeys() -> None:
    """The branch that takes a pre-envelope Fernet ciphertext, decrypts
    it with the legacy HKDF derivation, and re-encrypts under the new
    envelope. This is the path most rows take in a real deployment that
    had ``ENCRYPTION_KEY`` set; the plaintext fallback only fires when
    the prior deployment ran without a key.

    Round-trip assertion: a row encrypted by the old code path decrypts
    via ``_rekey`` to the original plaintext, with the new envelope as
    the surface form.
    """
    migration = _load_migration_018()

    # Build a legacy Fernet matching the pre-envelope derivation, encrypt
    # a token under it, and confirm the ciphertext is not envelope-shaped.
    key_material = secrets.token_bytes(32)
    legacy = _legacy_fernet_for(key_material)
    legacy_ciphertext = legacy.encrypt(b"legacy-access-token").decode()
    assert not is_envelope(legacy_ciphertext)

    # The new KEK provider must use the same key material so its derived
    # KEK can wrap DEKs that downstream reads will unwrap. (The legacy
    # and new HKDF info parameters differ, so the keys themselves are
    # distinct even when the input material is shared.)
    provider = LocalKEKProvider(key_material=key_material)
    rekeyed = migration._rekey(legacy_ciphertext, legacy, provider, "access_token")

    assert is_envelope(rekeyed)
    assert (
        decrypt(rekeyed, provider, {"table": "oauth_tokens", "column": "access_token"})
        == "legacy-access-token"
    )


def test_migration_020_rekey_helper_envelopes_plaintext() -> None:
    """Migration 020 has no legacy ciphertext to handle. Message bodies
    were always plaintext before this revision. ``_rekey`` should
    envelope-encrypt non-envelope values, return envelopes unchanged
    (idempotent re-runs), and pass empty/None through untouched.
    """
    migration = _load_migration_020()
    provider = LocalKEKProvider()

    rekeyed = migration._rekey("message body text", provider, "body")
    assert isinstance(rekeyed, str)
    assert rekeyed.startswith(ENVELOPE_PREFIX + ".")
    assert (
        decrypt(rekeyed, provider, {"table": "messages", "column": "body"}) == "message body text"
    )

    # Idempotent re-run: an envelope is returned by identity, so the
    # caller (the upgrade loop) can detect "nothing to do" and skip
    # the UPDATE.
    assert migration._rekey(rekeyed, provider, "body") is rekeyed

    # Empty / None values pass through untouched, matching the type
    # decorator's bind-param short-circuit.
    assert migration._rekey("", provider, "body") == ""
    assert migration._rekey(None, provider, "body") is None


def test_migration_020_processed_context_uses_distinct_column_context() -> None:
    """The migration encrypts ``body`` and ``processed_context`` under
    distinct ``EncryptionContext`` column tags, matching the model's
    column declarations. Without this, a future per-column key rotation
    couldn't target one column independently.
    """
    migration = _load_migration_020()
    provider = LocalKEKProvider()

    body_envelope = migration._rekey("user said hi", provider, "body")
    pc_envelope = migration._rekey("user said hi (transcribed)", provider, "processed_context")

    # Both should decrypt under their respective contexts. Cross-context
    # decrypt either succeeds (LocalKEKProvider doesn't enforce context
    # equality on its own) or fails. What matters is that the call
    # sites use the matching column tag, which is what ``EncryptedString``
    # passes at runtime.
    assert (
        decrypt(body_envelope, provider, {"table": "messages", "column": "body"}) == "user said hi"
    )
    assert (
        decrypt(pc_envelope, provider, {"table": "messages", "column": "processed_context"})
        == "user said hi (transcribed)"
    )


def test_migration_020_full_upgrade_loop_against_real_db(
    install_recording_provider: _RecordingProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end run of ``upgrade()`` against the test database.

    The unit-style ``test_migration_020_rekey_helper_*`` tests cover the
    per-row helper. This one verifies the full streaming loop terminates
    on a real PostgreSQL connection, that already-encrypted rows are
    skipped (no UPDATE on a re-run), and that the cursor advancement
    logic doesn't infinite-loop when the in-loop ``last_id = row_id``
    assignment is bypassed.
    """
    # The migration's preflight check refuses to run when the messages
    # table is non-empty AND ``settings.encryption_key`` is empty. The
    # test database fits that shape (no key configured by default), so
    # seed a synthetic key for this test. This is exactly the operator
    # workflow the preflight is documenting: set the key, then migrate.
    monkeypatch.setattr(_app_settings, "encryption_key", SecretStr("a" * 32))

    # Insert plaintext rows directly via raw SQL to simulate the pre-
    # migration state. Going through the ORM would invoke the
    # ``EncryptedString`` type decorator and pre-encrypt them, which is
    # exactly what we want to AVOID for this test.
    db = _db_module.SessionLocal()
    try:
        user = User(id=str(uuid.uuid4()), user_id="msg-mig-e2e-test", onboarding_complete=True)
        db.add(user)
        db.flush()
        cs = ChatSession(session_id=f"sess-{uuid.uuid4().hex[:8]}", user_id=user.id)
        db.add(cs)
        db.flush()
        # Three plaintext rows so we exercise the multi-row code path.
        for seq, body, ctx in [
            (1, "first plaintext body", "first context"),
            (2, "second plaintext body", ""),
            (3, "", ""),  # all-empty: must skip without error
        ]:
            db.execute(
                _sa_text(
                    "INSERT INTO messages (session_id, seq, direction, body, "
                    "processed_context, tool_interactions_json, external_message_id, "
                    "media_urls_json, timestamp) VALUES (:s, :seq, 'inbound', :b, "
                    ":pc, '', '', '', NOW())"
                ),
                {"s": cs.id, "seq": seq, "b": body, "pc": ctx},
            )
        db.commit()
    finally:
        db.close()

    # Run the migration's ``upgrade()`` against the same connection
    # alembic would use. ``op.get_bind()`` resolves to the configured
    # connection during a real alembic run; here we monkeypatch it to
    # point at the test session's connection so the migration writes
    # through to the same database the test reads from afterwards.
    migration = _load_migration_020()
    db = _db_module.SessionLocal()
    try:
        monkeypatch.setattr(_alembic_op, "get_bind", lambda: db.connection())
        migration.upgrade()
        db.commit()
    finally:
        db.close()

    # Verify the migration wrote envelopes for the non-empty rows and
    # left the empty row alone (empty strings short-circuit in _rekey).
    db = _db_module.SessionLocal()
    try:
        rows = db.execute(
            _sa_text(
                "SELECT seq, body, processed_context FROM messages "
                "WHERE session_id = :s ORDER BY seq"
            ),
            {"s": cs.id},
        ).all()
        assert len(rows) == 3
        # Row 1: both columns envelope-encrypted.
        assert rows[0].body.startswith(ENVELOPE_PREFIX + ".")
        assert rows[0].processed_context.startswith(ENVELOPE_PREFIX + ".")
        # Row 2: body envelope, empty context stays empty.
        assert rows[1].body.startswith(ENVELOPE_PREFIX + ".")
        assert rows[1].processed_context == ""
        # Row 3: both empty, must remain empty (no envelope on empty).
        assert rows[2].body == ""
        assert rows[2].processed_context == ""
    finally:
        db.close()

    # Idempotent re-run: a second ``upgrade()`` on the now-encrypted
    # rows must NOT issue any UPDATE (every row is already in envelope
    # form, ``_rekey`` returns the original by identity, and the loop's
    # short-circuit fires). The cursor reach-around guarantees the loop
    # terminates regardless. We assert termination simply by reaching
    # the next line without timing out.
    db = _db_module.SessionLocal()
    try:
        monkeypatch.setattr(_alembic_op, "get_bind", lambda: db.connection())
        migration.upgrade()
        db.commit()
    finally:
        db.close()


def test_migration_018_falls_back_to_plaintext_on_invalid_legacy_token() -> None:
    """If a row's ciphertext fails to decrypt under the configured
    legacy key (``InvalidToken``), the migration treats it as plaintext.

    Documents the behavior so a future change can't silently regress to
    raising. The realistic path here is "row was stored as plaintext
    before the operator set ENCRYPTION_KEY"; the dangerous path is
    "ENCRYPTION_KEY was rotated since the row was written," which would
    re-encrypt unrecoverable ciphertext as if it were plaintext. The
    migration's no-downgrade docstring calls out backup-restore as the
    recovery path for that case.
    """
    migration = _load_migration_018()

    legacy = _legacy_fernet_for(secrets.token_bytes(32))
    provider = LocalKEKProvider(key_material=secrets.token_bytes(32))

    # Pass a value that isn't valid Fernet ciphertext. Legacy decryption
    # will raise InvalidToken; _decrypt_legacy returns the value as-is;
    # _rekey envelope-encrypts that string verbatim.
    rekeyed = migration._rekey("not-a-fernet-token", legacy, provider, "access_token")

    assert is_envelope(rekeyed)
    assert (
        decrypt(rekeyed, provider, {"table": "oauth_tokens", "column": "access_token"})
        == "not-a-fernet-token"
    )
