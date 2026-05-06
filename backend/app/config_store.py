"""Persistent settings store: DB-backed runtime configuration.

Replaces the legacy ``data/config.json`` flow. Same contract from the
caller's point of view (load on boot, save when admin updates a value),
but the bytes live in a Postgres table instead of a file on a volume
mount. This kills several sharp edges:

* Silent failures from a missing/mis-mounted volume. If the store can't
  load, it raises ``ConfigStoreError`` instead of returning ``{}`` and
  letting the app boot with empty defaults.
* Plaintext secrets on disk. Keys in ``_SECRET_SETTINGS`` are
  envelope-encrypted via the existing ``KEKProvider`` before write.
* Multi-replica safety. Two replicas saving to a file would race; a DB
  upsert is atomic per call.

Bootstrap secrets (``DATABASE_URL``, ``JWT_SECRET``, ``ENCRYPTION_KEY``,
``KMS_KEY_ARN``) stay in env by definition: the store needs the DB to
read, so any setting required to *reach* the DB cannot live there.

Env vars still take precedence over stored values when applied to the
``settings`` singleton. That's the emergency-override knob operators
already rely on.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import TextClause

from backend.app.config import (
    PERSISTABLE_SETTINGS,
    settings,
    update_settings,
)
from backend.app.security import encryption as _encryption
from backend.app.security.encryption import KEKProvider

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public constants & helpers
# ---------------------------------------------------------------------------

# Keys whose values are sensitive and must be encrypted at rest. The
# allowlist is the source of truth for both the store (encrypt/decrypt)
# and any GET endpoint that wants to mask values in API responses.
_SECRET_SETTINGS: frozenset[str] = frozenset(
    {
        "telegram_bot_token",
        "telegram_webhook_secret",
        "linq_api_token",
        "linq_webhook_signing_secret",
        "bluebubbles_password",
        "dropbox_access_token",
        "google_drive_credentials_json",
    }
)

# Sentinel returned in GET responses for secret keys that have a value.
# A round-trip PUT carrying this value is treated as "no change for this
# key" (see ``strip_unchanged_secrets``), so the UI can re-submit a form
# without the user having to retype every secret.
MASK = "********"


def is_secret(key: str) -> bool:
    """Return True if *key* names a sensitive setting.

    Caller's responsibility to gate API responses through
    ``mask_for_response`` for any value backed by such a key.
    """
    return key in _SECRET_SETTINGS


def mask_for_response(key: str, value: str) -> str:
    """Mask a value for inclusion in a GET response.

    Empty stays empty so the UI can distinguish "not configured" from
    "configured, hidden". A non-empty secret becomes ``MASK``; a
    non-secret value is returned unchanged.
    """
    if not value:
        return ""
    return MASK if is_secret(key) else value


def strip_unchanged_secrets(updates: Mapping[str, str]) -> dict[str, str]:
    """Drop secret keys whose value is the masking sentinel.

    The UI's edit flow renders ``MASK`` for existing secrets and only
    replaces it when the admin types a new value. If the form is
    re-submitted unchanged, the request body still contains ``MASK`` for
    those keys; persisting that literal string would corrupt real
    secrets. This helper is the single chokepoint for that semantics.
    """
    return {k: v for k, v in updates.items() if not (is_secret(k) and v == MASK)}


def apply_to_settings(persisted: Mapping[str, Any]) -> dict[str, str]:
    """Apply persisted values to the live ``settings`` singleton.

    Real environment variables win over the store; non-persistable keys
    are skipped. Returns the dict of keys actually applied so the caller
    can log the boot-time hydration.
    """
    applied: dict[str, str] = {}
    for key, value in persisted.items():
        if key not in PERSISTABLE_SETTINGS:
            continue
        if os.environ.get(key.upper()):
            continue
        applied[key] = str(value)
    if applied:
        update_settings(applied)
    return applied


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ConfigStoreError(RuntimeError):
    """The settings backend is unreachable or returned invalid data.

    Raised instead of silently returning ``{}`` so a missing volume,
    missing table, or decryption failure surfaces at startup with a
    real error rather than crashing 30 lines deeper in
    ``_verify_llm_settings``.
    """


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class SettingsStore(Protocol):
    """Persistent backend for runtime-configurable settings."""

    async def load(self) -> dict[str, str]:
        """Return all persisted settings.

        An empty dict means the backend is reachable but has no rows.
        Backend failures (DB down, missing table, decryption error)
        raise ``ConfigStoreError`` so they don't masquerade as "no
        config saved yet".
        """
        ...

    async def save(self, updates: Mapping[str, str], *, actor_user_id: str | None = None) -> None:
        """Atomically merge *updates* into persisted state.

        ``actor_user_id`` is recorded against each updated row so the
        admin UI can show "last changed by" without a join against the
        audit log.
        """
        ...

    async def delete(self, keys: Iterable[str]) -> None:
        """Remove keys, reverting them to env or Pydantic default."""
        ...


# ---------------------------------------------------------------------------
# DB-backed store
# ---------------------------------------------------------------------------


# Dual-API rollout (issue #1175, follows the IdempotencyStore pilot in
# #1199). Internal logic is factored into pure module-level helpers so
# the sync and async methods stay in lockstep without a class
# hierarchy. Each existing public method keeps its plain name; the new
# ``*_async`` peer uses real async DB access via the configured async
# session factory (default: ``AsyncSessionLocal``).
#
# These helpers are intentionally not parameterized over the bind type:
# they return raw-SQL TextClauses and pure Python payloads, which both
# ``Session.execute`` and ``AsyncSession.execute`` accept.
def _load_select_sql() -> TextClause:
    """Builder shared by ``load`` / ``load_async``."""
    return text("SELECT key, value, is_secret FROM app_settings")


def _save_upsert_sql() -> TextClause:
    """Builder shared by ``save`` / ``save_async``.

    One round-trip: ON CONFLICT upsert for the whole batch.
    """
    return text(
        """
        INSERT INTO app_settings (key, value, is_secret, updated_by_user_id)
        VALUES (:key, :value, :is_secret, :actor)
        ON CONFLICT (key) DO UPDATE SET
            value = EXCLUDED.value,
            is_secret = EXCLUDED.is_secret,
            updated_at = NOW(),
            updated_by_user_id = EXCLUDED.updated_by_user_id
        """
    )


def _delete_sql() -> TextClause:
    """Builder shared by ``delete`` / ``delete_async``."""
    return text("DELETE FROM app_settings WHERE key = ANY(:keys)")


def _encryption_context() -> _encryption.EncryptionContext:
    """Encryption context shared by both sync and async paths.

    Module-level so the sync and async methods see identical (table,
    column) bindings without going through an instance method.
    """
    return {"table": "app_settings", "column": "value"}


def _build_save_rows(
    updates: Mapping[str, str],
    kek: KEKProvider,
    *,
    actor_user_id: str | None,
) -> list[dict[str, Any]]:
    """Validate updates and prepare rows for the upsert statement.

    Pure helper so ``save`` / ``save_async`` use identical
    persistable-key validation and secret-encryption semantics. Raises
    ``ValueError`` for keys outside ``PERSISTABLE_SETTINGS`` so the
    failure mode does not depend on which code path the caller picked.
    """
    rows: list[dict[str, Any]] = []
    ctx = _encryption_context()
    for key, value in updates.items():
        if key not in PERSISTABLE_SETTINGS:
            raise ValueError(
                f"{key!r} is not a persistable setting (allowed: {sorted(PERSISTABLE_SETTINGS)})"
            )
        secret = is_secret(key)
        stored_value = _encryption.encrypt(value, kek, ctx) if secret and value else value
        rows.append(
            {
                "key": key,
                "value": stored_value,
                "is_secret": secret,
                "actor": actor_user_id,
            }
        )
    return rows


def _decode_load_rows(rows: Iterable[Row[Any]], kek: KEKProvider) -> dict[str, str]:
    """Decrypt secret values and assemble the load() return dict.

    Pure helper so ``load`` / ``load_async`` use identical decryption
    semantics. Empty values short-circuit to "" without a decrypt call,
    matching the original behavior. Decryption failures are wrapped in
    ``ConfigStoreError`` so the failure mode is the same regardless of
    which path discovered the bad row.

    Accepts ``Row[Any]`` because SQLAlchemy's ``Result.all()`` returns
    ``Sequence[Row[Any]]`` for both sync and async paths; positional
    unpacking matches the (key, value, is_secret) shape of the SELECT.
    """
    result: dict[str, str] = {}
    ctx = _encryption_context()
    for row in rows:
        key, value, is_secret_flag = row[0], row[1], row[2]
        if not value:
            result[key] = ""
            continue
        if is_secret_flag:
            try:
                result[key] = _encryption.decrypt(value, kek, ctx)
            except Exception as exc:
                raise ConfigStoreError(f"Failed to decrypt app_settings.{key}: {exc}") from exc
        else:
            result[key] = value
    return result


class DbSettingsStore:
    """Stores settings in the ``app_settings`` table.

    Secret keys (per ``_SECRET_SETTINGS``) are envelope-encrypted via
    the configured ``KEKProvider`` before insertion. Decryption happens
    on read. Non-secret keys are stored verbatim.

    Async-only as of issue #1160. The store resolves
    ``AsyncSessionLocal`` lazily so a store instantiated at import time
    picks up test rebinding of the ``async_db`` fixture's session
    factory. ``*_async`` aliases are kept as thin wrappers in case
    out-of-tree callers still reference the suffix.
    """

    _CONTEXT_TABLE = "app_settings"
    _CONTEXT_COLUMN = "value"

    def __init__(
        self,
        kek_provider: KEKProvider,
        async_session_factory: Callable[[], AsyncSession] | None = None,
    ) -> None:
        """Construct a store bound to *async_session_factory*.

        When the factory is ``None`` (the production default), each
        call resolves the singleton ``AsyncSessionLocal`` at call time
        so test fixtures rebinding ``_async_session_factory`` are
        picked up. Tests that drive the API through a per-test
        transaction can pass the rebound ``async_sessionmaker`` from
        the ``async_db`` fixture explicitly.
        """
        self._kek = kek_provider
        self._async_session_factory = async_session_factory

    def _resolve_factory(self) -> Callable[[], AsyncSession]:
        """Return the async session factory to use for this call.

        Deferred lookup: the constructor default is ``None`` so that a
        store instantiated before the async engine has booted (e.g. at
        import time) doesn't capture a stale factory.
        """
        if self._async_session_factory is not None:
            return self._async_session_factory
        # Local import to avoid a cycle with backend.app.database at
        # module load (config_store is imported during settings boot
        # via the lifespan handler, before some test fixtures have run).
        from backend.app.database import AsyncSessionLocal

        return AsyncSessionLocal

    async def load(self) -> dict[str, str]:
        factory = self._resolve_factory()
        try:
            async with factory() as db:
                rows = (await db.execute(_load_select_sql())).all()
        except Exception as exc:
            raise ConfigStoreError(f"Failed to query app_settings: {exc}") from exc

        return _decode_load_rows(rows, self._kek)

    async def load_async(self) -> dict[str, str]:
        """Deprecated alias of :meth:`load`."""
        return await self.load()

    async def save(self, updates: Mapping[str, str], *, actor_user_id: str | None = None) -> None:
        if not updates:
            return
        rows = _build_save_rows(updates, self._kek, actor_user_id=actor_user_id)
        factory = self._resolve_factory()
        async with factory() as db:
            await db.execute(_save_upsert_sql(), rows)
            await db.commit()

    async def save_async(
        self, updates: Mapping[str, str], *, actor_user_id: str | None = None
    ) -> None:
        """Deprecated alias of :meth:`save`."""
        await self.save(updates, actor_user_id=actor_user_id)

    async def delete(self, keys: Iterable[str]) -> None:
        keys_list = list(keys)
        if not keys_list:
            return
        factory = self._resolve_factory()
        async with factory() as db:
            await db.execute(_delete_sql(), {"keys": keys_list})
            await db.commit()

    async def delete_async(self, keys: Iterable[str]) -> None:
        """Deprecated alias of :meth:`delete`."""
        await self.delete(keys)

    def _encryption_context(self) -> _encryption.EncryptionContext:
        # Kept for backward-compat with any subclass / external caller
        # that may have referenced the instance method directly. The
        # module-level ``_encryption_context()`` is the source of truth.
        return _encryption_context()


# ---------------------------------------------------------------------------
# JSON-file store (kept for backwards compat / file-based deployments)
# ---------------------------------------------------------------------------


class JsonFileSettingsStore:
    """Stores settings in a JSON file on disk.

    Wraps the legacy ``data/config.json`` behavior. Unlike the prior
    ``load_persistent_config`` helper, ``load()`` raises
    ``ConfigStoreError`` when the file is missing rather than returning
    an empty dict: a missing config file in production is the exact
    failure mode that masked the bug this module was created to fix.
    Set ``allow_missing=True`` for true "first boot, file may not exist
    yet" semantics.
    """

    def __init__(self, path: Path, *, allow_missing: bool = False) -> None:
        self._path = path
        self._allow_missing = allow_missing

    async def load(self) -> dict[str, str]:
        if not self._path.is_file():
            if self._allow_missing:
                return {}
            raise ConfigStoreError(
                f"Settings file does not exist: {self._path}. "
                f"Either save settings via the admin UI to create it, "
                f"or set SETTINGS_STORE=db to use the DB-backed store."
            )
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise ConfigStoreError(f"Failed to read {self._path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigStoreError(f"{self._path} is not a JSON object at the top level")
        return {k: str(v) for k, v in data.items()}

    async def save(self, updates: Mapping[str, str], *, actor_user_id: str | None = None) -> None:
        del actor_user_id  # JSON file has no audit column
        if not updates:
            return
        existing: dict[str, str] = {}
        if self._path.is_file():
            try:
                existing = json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                existing = {}
        existing.update(updates)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")

    async def delete(self, keys: Iterable[str]) -> None:
        keys_list = list(keys)
        if not keys_list or not self._path.is_file():
            return
        try:
            existing = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for key in keys_list:
            existing.pop(key, None)
        self._path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Factory and one-shot import
# ---------------------------------------------------------------------------


_store: SettingsStore | None = None


def _config_json_path() -> Path:
    """Return the legacy ``data/config.json`` path.

    Kept for backwards-compat import on first boot of the DB store, and
    for ``SETTINGS_STORE=file`` mode. ``data_dir`` typically points at
    ``data/users``; the config file lives one level up so it sits
    directly inside the mounted ``data/`` volume.
    """
    return Path(settings.data_dir).parent / "config.json"


def get_settings_store() -> SettingsStore:
    """Return the active store, instantiating lazily.

    Selection: ``SETTINGS_STORE=db`` (default) → ``DbSettingsStore``.
    ``SETTINGS_STORE=file`` → ``JsonFileSettingsStore``. Cached so
    subsequent calls return the same instance; tests use
    ``reset_settings_store()`` to clear.
    """
    global _store
    if _store is not None:
        return _store

    backend = settings.settings_store.lower()
    if backend == "file":
        _store = JsonFileSettingsStore(_config_json_path())
    elif backend == "db":
        from backend.app.auth.loader import get_kek_provider

        # Async session factory is resolved lazily on each call so test
        # rebinding of ``_async_session_factory`` is picked up.
        _store = DbSettingsStore(get_kek_provider())
    else:
        raise ConfigStoreError(
            f"Unknown SETTINGS_STORE value: {backend!r} (expected 'db' or 'file')"
        )
    return _store


def reset_settings_store() -> None:
    """Clear the cached store. Test-only."""
    global _store
    _store = None


async def import_legacy_config_json(store: SettingsStore) -> dict[str, str]:
    """One-shot import of legacy ``data/config.json`` into the store.

    Idempotent: a no-op if the store already has any persistable rows
    or if the legacy file doesn't exist. Returns the dict of keys that
    were imported (empty if nothing happened).

    Called once at lifespan startup so deployments upgrading from the
    file backend don't lose their existing settings.
    """
    legacy_path = _config_json_path()
    if not legacy_path.is_file():
        return {}

    try:
        current = await store.load()
    except ConfigStoreError:
        # Store unreachable. Don't import; let the caller surface the
        # underlying failure.
        return {}

    if any(k in PERSISTABLE_SETTINGS for k in current):
        # Store has settings already; nothing to import.
        return {}

    try:
        legacy_data = json.loads(legacy_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Skipping legacy config.json import: %s", exc)
        return {}

    if not isinstance(legacy_data, dict):
        logger.warning(
            "Skipping legacy config.json import: %s is not a JSON object",
            legacy_path,
        )
        return {}

    to_import: dict[str, str] = {
        str(k): str(v) for k, v in legacy_data.items() if k in PERSISTABLE_SETTINGS
    }
    if not to_import:
        return {}

    await store.save(to_import)
    logger.info(
        "Imported %d setting(s) from legacy %s into the settings store: %s",
        len(to_import),
        legacy_path,
        sorted(to_import),
    )
    return to_import


__all__ = [
    "MASK",
    "ConfigStoreError",
    "DbSettingsStore",
    "JsonFileSettingsStore",
    "SettingsStore",
    "apply_to_settings",
    "get_settings_store",
    "import_legacy_config_json",
    "is_secret",
    "mask_for_response",
    "reset_settings_store",
    "strip_unchanged_secrets",
]
