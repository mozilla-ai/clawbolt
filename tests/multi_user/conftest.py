"""Fixtures for the ``AUTH_MODE=multi_user`` test suite.

The root ``tests/conftest.py`` owns the database: schema creation, engine
rebinding, per-test truncation, and store resets all apply here unchanged.
This file adds what only multi-user tests need, and overrides the two
fixtures whose single-user shape does not fit:

* ``client`` builds a second app from ``create_app()`` with the mode
  patched, rather than reusing the module-level single-user ``app``.
* ``test_user`` uses a ``google_`` user_id, matching what the OAuth flow
  produces and what the admin and account tests assert against.

The sync ``db_session`` fixture exists because much of this suite does its
setup and assertions through a plain ``Session`` rather than the async
stack. It runs on its own engine and its own connection, so a row it
commits is visible to async reads only after that commit lands, which is
the same cross-API caveat the root conftest documents for ``async_db``.
"""

from __future__ import annotations

import contextlib
import datetime
import uuid
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from backend.app.auth.dependencies import get_current_user
from backend.app.config import settings
from backend.app.main import create_app
from backend.app.models import Subscription, UsageQuota, User
from tests.conftest import _TEST_DB_NAME

# One app for the whole package, built with the multi-user surface mounted.
# A module-level singleton rather than a fixture because tests reach for it
# directly (``app.dependency_overrides[...] = ...``) and every one of those
# has to land on the same object the client serves.
_saved_auth_mode = settings.auth_mode
settings.auth_mode = "multi_user"
try:
    MULTI_USER_APP = create_app()
finally:
    settings.auth_mode = _saved_auth_mode

# ``+psycopg`` names psycopg3 explicitly, which is what the ``dev`` extra
# declares (``psycopg[binary]``). A bare ``postgresql://`` resolves to
# SQLAlchemy's default psycopg2 dialect, which is not a declared dependency.
_TEST_DB_URL = f"postgresql+psycopg://clawbolt:clawbolt@localhost:5432/{_TEST_DB_NAME}"

# NullPool: every session gets its own connection and closes it on exit.
# A pooled connection surviving a test can still hold a lock (or an open
# transaction) when the root conftest TRUNCATEs between tests, which
# showed up as the truncate blocking and the next test's inserts failing
# their foreign keys against rows it could not see.
_SYNC_TEST_ENGINE = create_sync_engine(_TEST_DB_URL, poolclass=NullPool)
_SYNC_TEST_SESSION_FACTORY = sessionmaker(
    bind=_SYNC_TEST_ENGINE,
    autocommit=False,
    autoflush=False,
)


def get_test_sync_engine() -> Engine:
    """Return the standalone sync engine used only by this suite."""
    return _SYNC_TEST_ENGINE


def open_test_db_session() -> Session:
    """Open a standalone sync Session against the test database."""
    return _SYNC_TEST_SESSION_FACTORY()


@pytest.fixture(autouse=True)
def _multi_user_mode() -> Generator[None]:
    """Run every test in this directory under AUTH_MODE=multi_user."""
    with patch.object(settings, "auth_mode", "multi_user"):
        yield


@pytest.fixture(autouse=True)
def _open_registration() -> Generator[None]:
    """Default to open registration so existing signup flows work."""
    original = settings.registration_mode
    settings.registration_mode = "open"
    yield
    settings.registration_mode = original


@pytest.fixture()
def db_session() -> Generator[Session]:
    """Fresh standalone sync Session per test."""
    session = open_test_db_session()
    try:
        yield session
    finally:
        with contextlib.suppress(Exception):
            session.rollback()
        session.close()


@pytest.fixture()
def test_user(tmp_path: Path) -> User:
    """Create a test user committed to the test database."""
    db = open_test_db_session()
    try:
        user = User(
            id=str(uuid.uuid4()),
            user_id="google_test123",
            phone="+1234567890",
            channel_identifier="123456789",
            preferred_channel="telegram",
            onboarding_complete=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        db.expunge(user)
    finally:
        db.close()

    user_dir = tmp_path / str(user.id)
    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / "sessions").mkdir(exist_ok=True)
    (user_dir / "memory").mkdir(exist_ok=True)
    (user_dir / "estimates").mkdir(exist_ok=True)
    (user_dir / "heartbeat").mkdir(exist_ok=True)
    return user


@pytest.fixture()
def test_subscription(db_session: Session, test_user: User) -> Subscription:
    """Create a test subscription (free plan, admin role)."""
    sub = Subscription(
        user_id=test_user.id,
        role="admin",
        plan="free",
        status="active",
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)
    return sub


@pytest.fixture()
def test_quota(db_session: Session, test_user: User) -> UsageQuota:
    """Create a test usage quota for the current month."""
    now = datetime.datetime.now(datetime.UTC)
    period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    quota = UsageQuota(
        user_id=test_user.id,
        period_start=period_start,
        messages_used=0,
        messages_limit=1000,
        tokens_used=0,
        tokens_limit=1_000_000,
    )
    db_session.add(quota)
    db_session.commit()
    db_session.refresh(quota)
    return quota


class _SyncToAsyncSessionProxy:
    """Minimal AsyncSession-shaped proxy over a sync ``Session``."""

    def __init__(self, sync_session: Session) -> None:
        self._sync = sync_session

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        return self._sync.execute(*args, **kwargs)

    async def commit(self) -> None:
        self._sync.commit()

    async def rollback(self) -> None:
        self._sync.rollback()

    async def close(self) -> None:
        return None

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        return self._sync.get(*args, **kwargs)

    async def delete(self, *args: Any, **kwargs: Any) -> None:
        self._sync.delete(*args, **kwargs)

    async def refresh(self, *args: Any, **kwargs: Any) -> None:
        self._sync.refresh(*args, **kwargs)

    async def flush(self, *args: Any, **kwargs: Any) -> None:
        self._sync.flush(*args, **kwargs)

    def add(self, instance: Any) -> None:
        self._sync.add(instance)

    def add_all(self, instances: list[Any]) -> None:
        self._sync.add_all(instances)

    def expunge(self, instance: Any) -> None:
        self._sync.expunge(instance)


@pytest.fixture()
def multi_user_app() -> FastAPI:
    """The package's multi-user app, as a fixture for tests that prefer one."""
    return MULTI_USER_APP


@pytest.fixture()
def client(test_user: User) -> Generator[TestClient]:
    """Test client for the multi-user app, with auth overridden."""
    multi_user_app = MULTI_USER_APP
    multi_user_app.dependency_overrides[get_current_user] = lambda: test_user
    mock_manager = MagicMock()
    mock_manager.start_all = AsyncMock(return_value=[])
    mock_manager.stop_all = AsyncMock()
    mock_manager.channels = {}
    settings_store_mock = MagicMock()
    settings_store_mock.load = AsyncMock(return_value={})
    settings_store_mock.save = AsyncMock()
    settings_store_mock.delete = AsyncMock()
    with (
        patch("backend.app.main.get_settings_store", return_value=settings_store_mock),
        patch("backend.app.main.import_legacy_config_json", new_callable=AsyncMock),
        patch("backend.app.main.apply_to_settings", return_value={}),
        patch("backend.app.main.load_dotenv"),
        patch("backend.app.main._verify_llm_settings", new_callable=AsyncMock),
        patch("backend.app.main._enforce_single_channel", new_callable=AsyncMock),
        patch("backend.app.main.heartbeat_scheduler"),
        patch("backend.app.main.oauth_refresh_scheduler"),
        patch("backend.app.main.health_monitor"),
        patch("backend.app.main.start_alert_flusher"),
        patch("backend.app.main.stop_alert_flusher"),
        patch("backend.app.main.get_manager", return_value=mock_manager),
        TestClient(multi_user_app) as c,
    ):
        yield c

    multi_user_app.dependency_overrides.pop(get_current_user, None)
