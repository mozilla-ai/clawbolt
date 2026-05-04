"""Tests for the SettingsStore module.

The integration test ``test_db_store_save_and_load_round_trips`` is
required by the design: the previous attempt at DB-backed config
(``platform_configs``, dropped in premium migration p020) shipped a
write path that was never wired into the read path. This test proves
that ``save`` then ``load`` returns the exact value, including for
encrypted secret keys, which is the load-bearing guarantee for the
admin-UI-saves-survive-restart contract.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import Engine

from backend.app.config import settings
from backend.app.config_store import (
    MASK,
    ConfigStoreError,
    DbSettingsStore,
    JsonFileSettingsStore,
    apply_to_settings,
    is_secret,
    mask_for_response,
    strip_unchanged_secrets,
)
from backend.app.security.encryption import LocalKEKProvider, is_envelope

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_is_secret_covers_known_secret_keys() -> None:
    assert is_secret("telegram_bot_token")
    assert is_secret("bluebubbles_password")
    assert is_secret("dropbox_access_token")
    assert is_secret("google_drive_credentials_json")


def test_is_secret_excludes_non_secret_keys() -> None:
    assert not is_secret("llm_provider")
    assert not is_secret("llm_model")
    assert not is_secret("telegram_allowed_chat_id")


def test_mask_for_response_masks_only_set_secrets() -> None:
    # Secret + value → MASK (UI shows "configured, hidden").
    assert mask_for_response("telegram_bot_token", "real-token") == MASK
    # Secret + empty → empty (UI shows "not configured").
    assert mask_for_response("telegram_bot_token", "") == ""
    # Non-secret → passthrough regardless of emptiness.
    assert mask_for_response("llm_provider", "anthropic") == "anthropic"
    assert mask_for_response("llm_provider", "") == ""


def test_strip_unchanged_secrets_drops_mask_round_trips() -> None:
    raw = {
        "telegram_bot_token": MASK,  # round-trip; drop
        "telegram_allowed_chat_id": "111",  # plain value; keep
        "bluebubbles_password": "fresh-pw",  # new secret; keep
        "linq_api_token": MASK,  # round-trip; drop
    }
    result = strip_unchanged_secrets(raw)
    assert result == {
        "telegram_allowed_chat_id": "111",
        "bluebubbles_password": "fresh-pw",
    }


# ---------------------------------------------------------------------------
# apply_to_settings: env-precedence semantics
# ---------------------------------------------------------------------------


def test_apply_to_settings_skips_non_persistable_keys() -> None:
    applied = apply_to_settings({"definitely_not_a_setting": "x"})
    assert applied == {}


def test_apply_to_settings_env_var_wins() -> None:
    """When an env var is set (even to empty? — no, must be non-empty), it wins."""
    original = settings.llm_provider
    try:
        with patch.dict("os.environ", {"LLM_PROVIDER": "anthropic"}):
            applied = apply_to_settings({"llm_provider": "openai"})
        # Env present and non-empty → store value not applied.
        assert "llm_provider" not in applied
    finally:
        settings.llm_provider = original


def test_apply_to_settings_uses_store_when_env_unset() -> None:
    original = settings.llm_provider
    try:
        # Make sure env is clear.
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("LLM_PROVIDER", None)
            applied = apply_to_settings({"llm_provider": "anthropic"})
        assert applied == {"llm_provider": "anthropic"}
        assert settings.llm_provider == "anthropic"
    finally:
        settings.llm_provider = original


# ---------------------------------------------------------------------------
# JsonFileSettingsStore
# ---------------------------------------------------------------------------


def test_json_file_store_loads_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"llm_provider": "anthropic", "llm_model": "sonnet"}))
    store = JsonFileSettingsStore(path)
    assert store.load() == {"llm_provider": "anthropic", "llm_model": "sonnet"}


def test_json_file_store_missing_file_is_loud_by_default(tmp_path: Path) -> None:
    """Missing file raises ``ConfigStoreError`` instead of returning {}.

    This is the regression guard for the production bug that motivated
    this whole module: a silent ``{}`` from a missing file caused
    ``_verify_llm_settings`` to crash with ``LLMProvider('')`` rather
    than a clear "config file is missing" error.
    """
    store = JsonFileSettingsStore(tmp_path / "missing.json")
    with pytest.raises(ConfigStoreError, match="does not exist"):
        store.load()


def test_json_file_store_allow_missing_returns_empty(tmp_path: Path) -> None:
    """First-boot opt-in: ``allow_missing=True`` returns {}."""
    store = JsonFileSettingsStore(tmp_path / "missing.json", allow_missing=True)
    assert store.load() == {}


def test_json_file_store_save_creates_and_merges(tmp_path: Path) -> None:
    path = tmp_path / "data" / "config.json"
    store = JsonFileSettingsStore(path)
    store.save({"llm_provider": "anthropic"})
    assert json.loads(path.read_text()) == {"llm_provider": "anthropic"}
    store.save({"llm_model": "sonnet"})
    assert json.loads(path.read_text()) == {
        "llm_provider": "anthropic",
        "llm_model": "sonnet",
    }


def test_json_file_store_delete_removes_keys(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    store = JsonFileSettingsStore(path)
    store.save({"llm_provider": "x", "llm_model": "y"})
    store.delete(["llm_model"])
    assert json.loads(path.read_text()) == {"llm_provider": "x"}


# ---------------------------------------------------------------------------
# DbSettingsStore (real Postgres via the test fixture engine)
# ---------------------------------------------------------------------------


@pytest.fixture()
def _kek_provider() -> LocalKEKProvider:
    """Deterministic provider for round-trip tests."""
    return LocalKEKProvider(key_material=b"x" * 32)


@pytest.fixture()
def _db_store(_kek_provider: LocalKEKProvider) -> Generator[DbSettingsStore]:
    """Construct a store bound to the active ``SessionLocal``.

    The autouse ``_isolate_stores`` fixture rebinds ``SessionLocal`` to
    a per-test connection in a rolled-back transaction; using it here
    means our writes auto-clean between tests.
    """
    import backend.app.database as _db_module

    yield DbSettingsStore(_db_module.SessionLocal, _kek_provider)


def test_db_store_save_and_load_round_trips(_db_store: DbSettingsStore) -> None:
    """Save then load returns the exact value for both secret and non-secret keys.

    This is the integration guard the previous DB-backed attempt was
    missing: it proves the read and write halves agree end-to-end.
    """
    _db_store.save(
        {
            "llm_provider": "anthropic",
            "llm_model": "claude-sonnet-4-6",
            "telegram_bot_token": "real-secret-token",
        }
    )
    loaded = _db_store.load()
    assert loaded["llm_provider"] == "anthropic"
    assert loaded["llm_model"] == "claude-sonnet-4-6"
    # Decryption happened transparently.
    assert loaded["telegram_bot_token"] == "real-secret-token"


def test_db_store_secrets_are_envelope_encrypted_at_rest(
    _db_store: DbSettingsStore,
) -> None:
    """Verify the on-disk bytes are an envelope, not plaintext.

    Belt-and-suspenders: catches a future regression where someone
    forgets to call ``_encryption.encrypt`` before INSERT.
    """
    from sqlalchemy import text

    import backend.app.database as _db_module

    _db_store.save({"telegram_bot_token": "real-secret-token"})

    with _db_module.SessionLocal() as db:
        row = db.execute(
            text("SELECT value, is_secret FROM app_settings WHERE key='telegram_bot_token'")
        ).one()
    raw_value, is_secret_flag = row
    assert is_secret_flag is True
    assert "real-secret-token" not in raw_value
    assert is_envelope(raw_value)


def test_db_store_non_secret_stored_verbatim(
    _db_store: DbSettingsStore,
) -> None:
    from sqlalchemy import text

    import backend.app.database as _db_module

    _db_store.save({"llm_provider": "anthropic"})
    with _db_module.SessionLocal() as db:
        row = db.execute(
            text("SELECT value, is_secret FROM app_settings WHERE key='llm_provider'")
        ).one()
    assert row[0] == "anthropic"
    assert row[1] is False


def test_db_store_save_rejects_non_persistable_keys(_db_store: DbSettingsStore) -> None:
    with pytest.raises(ValueError, match="not a persistable setting"):
        _db_store.save({"definitely_not_a_setting": "x"})


def test_db_store_save_upserts(_db_store: DbSettingsStore) -> None:
    """Saving the same key twice updates the value, doesn't error on dup PK."""
    _db_store.save({"llm_provider": "anthropic"})
    _db_store.save({"llm_provider": "openai"})
    assert _db_store.load()["llm_provider"] == "openai"


def test_db_store_delete_removes_rows(_db_store: DbSettingsStore) -> None:
    _db_store.save({"llm_provider": "anthropic", "llm_model": "sonnet"})
    _db_store.delete(["llm_model"])
    loaded = _db_store.load()
    assert "llm_provider" in loaded
    assert "llm_model" not in loaded


def test_db_store_load_raises_on_missing_table(
    _kek_provider: LocalKEKProvider, _pg_engine: Engine
) -> None:
    """Backend-failure path raises ``ConfigStoreError``, not silently returns {}."""
    from sqlalchemy import text
    from sqlalchemy.orm import sessionmaker

    from backend.app.database import Base

    # Drop the table outside the autouse rollback so the store sees the
    # absence; recreate at the end so other tests aren't poisoned.
    with _pg_engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS app_settings CASCADE"))
    try:
        store = DbSettingsStore(
            sessionmaker(bind=_pg_engine, autocommit=False, autoflush=False),
            _kek_provider,
        )
        with pytest.raises(ConfigStoreError):
            store.load()
    finally:
        Base.metadata.tables["app_settings"].create(_pg_engine, checkfirst=True)


def test_db_store_save_records_actor(_db_store: DbSettingsStore) -> None:
    """``actor_user_id`` is recorded against the row for audit clarity."""
    from sqlalchemy import text

    import backend.app.database as _db_module
    from backend.app.models import User

    with _db_module.SessionLocal() as db:
        user = User(user_id="actor-user-store", channel_identifier="actor-tel")
        db.add(user)
        db.commit()
        actor_id = user.id

    _db_store.save({"llm_provider": "anthropic"}, actor_user_id=actor_id)

    with _db_module.SessionLocal() as db:
        row = db.execute(
            text("SELECT updated_by_user_id FROM app_settings WHERE key='llm_provider'")
        ).one()
    assert row[0] == actor_id
