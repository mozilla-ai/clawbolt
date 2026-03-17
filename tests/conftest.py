import uuid
from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import backend.app.database as _db_module
from backend.app.agent.approval import reset_approval_gate
from backend.app.agent.file_store import SessionState, StoredMessage, reset_stores
from backend.app.agent.memory_db import reset_memory_stores
from backend.app.agent.session_db import reset_session_stores
from backend.app.auth.dependencies import get_current_user
from backend.app.bus import message_bus
from backend.app.config import settings
from backend.app.database import Base, get_db
from backend.app.main import app
from backend.app.models import ChatSession, Message, User
from backend.app.services.rate_limiter import webhook_rate_limiter


@pytest.fixture(autouse=True)
def _isolate_stores(tmp_path: Path) -> Generator[None]:
    """Isolate file stores AND provide a per-test SQLite database.

    Patches the database module's engine and session factory so all code
    (including _get_or_create_user, get_current_user, etc.) uses a
    per-test in-memory SQLite DB.
    """
    # Set up per-test SQLite database (StaticPool so all connections share the same DB)
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(test_engine)
    test_session_factory = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

    old_engine = _db_module._engine
    old_factory = _db_module._SessionLocal

    _db_module._engine = test_engine
    _db_module._SessionLocal = test_session_factory

    # Set up per-test file store isolation
    with patch.object(settings, "data_dir", str(tmp_path)):
        reset_stores()
        reset_session_stores()
        reset_memory_stores()
        reset_approval_gate()
        yield

    # Restore
    _db_module._engine = old_engine
    _db_module._SessionLocal = old_factory
    reset_stores()
    reset_session_stores()
    reset_memory_stores()
    reset_approval_gate()
    test_engine.dispose()


@pytest.fixture()
async def test_user(tmp_path: Path) -> User:
    """Create a test user in the per-test SQLite database.

    Also creates the file-store directory structure so per-user stores
    (sessions, memory, etc.) can still write files during the hybrid period.
    """
    db = _db_module.SessionLocal()
    try:
        user = User(
            id=str(uuid.uuid4()),
            user_id="test-user-001",
            phone="+15551234567",
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

    # Ensure the user's file-store directory structure exists for per-user stores
    user_dir = tmp_path / str(user.id)
    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / "sessions").mkdir(exist_ok=True)
    (user_dir / "memory").mkdir(exist_ok=True)
    (user_dir / "estimates").mkdir(exist_ok=True)
    (user_dir / "heartbeat").mkdir(exist_ok=True)
    return user


def create_test_session(
    user_id: str,
    session_id: str = "test-conv",
    messages: list[StoredMessage] | None = None,
    is_active: bool = True,
    channel: str = "",
) -> SessionState:
    """Create a ChatSession row in the test DB and return a matching SessionState.

    Also creates Message rows for any provided StoredMessage objects.
    """
    from datetime import UTC, datetime

    db = _db_module.SessionLocal()
    try:
        cs = ChatSession(
            session_id=session_id,
            user_id=user_id,
            is_active=is_active,
            channel=channel,
            last_compacted_seq=0,
            created_at=datetime.now(UTC),
            last_message_at=datetime.now(UTC),
        )
        db.add(cs)
        db.flush()

        for msg in messages or []:
            ts = datetime.fromisoformat(msg.timestamp) if msg.timestamp else datetime.now(UTC)
            db.add(
                Message(
                    session_id=cs.id,
                    seq=msg.seq,
                    direction=msg.direction,
                    body=msg.body,
                    processed_context=msg.processed_context,
                    tool_interactions_json=msg.tool_interactions_json,
                    external_message_id=msg.external_message_id,
                    media_urls_json=msg.media_urls_json,
                    timestamp=ts,
                )
            )

        db.commit()
        db.refresh(cs)
        return SessionState(
            session_id=session_id,
            user_id=user_id,
            messages=list(messages or []),
            is_active=is_active,
            created_at=cs.created_at.isoformat(),
            last_message_at=cs.last_message_at.isoformat(),
            channel=channel,
        )
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _reset_bus_queues() -> Generator[None]:
    """Reset bus queues between tests so messages don't leak."""
    message_bus.reset()
    yield
    message_bus.reset()


@pytest.fixture()
def client(test_user: User) -> Generator[TestClient]:
    """FastAPI test client with overridden auth."""

    def _override_get_current_user() -> User:
        return test_user

    webhook_rate_limiter.reset()
    app.dependency_overrides[get_current_user] = _override_get_current_user
    with (
        patch("backend.app.main._verify_llm_settings", new_callable=AsyncMock),
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.start"),
        # Default allowlist to "*" (allow all) so tests are not blocked.
        # Individual allowlist tests override these values.
        patch("backend.app.channels.telegram.settings.telegram_allowed_chat_ids", "*"),
        patch("backend.app.channels.telegram.settings.telegram_allowed_usernames", ""),
        # Clear bot token so auto-derived webhook secret is empty for tests that
        # don't send a secret header
        patch("backend.app.channels.telegram.settings.telegram_bot_token", ""),
        # Disable message batching in tests: the async batcher creates
        # fire-and-forget tasks that outlive the synchronous TestClient lifecycle.
        patch("backend.app.agent.ingestion.settings.message_batch_window_ms", 0),
        TestClient(app) as c,
    ):
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# PostgreSQL test infrastructure
# ---------------------------------------------------------------------------

_TEST_DB_URL = "postgresql://clawbolt:clawbolt@localhost:5432/clawbolt_test"


@pytest.fixture(scope="session")
def postgres_engine() -> Generator:
    """Session-scoped Postgres engine. Uses testcontainers if available,
    otherwise falls back to a local Postgres instance."""
    try:
        from testcontainers.postgres import PostgresContainer

        with PostgresContainer("postgres:16-alpine") as pg:
            engine = create_engine(pg.get_connection_url(), pool_pre_ping=True)
            Base.metadata.create_all(engine)
            yield engine
            engine.dispose()
    except Exception:
        # Fallback: use a local Postgres if testcontainers isn't available
        # (e.g. no Docker in CI, use services: postgres instead)
        engine = create_engine(_TEST_DB_URL, pool_pre_ping=True)
        Base.metadata.create_all(engine)
        yield engine
        engine.dispose()


@pytest.fixture()
def db_session(postgres_engine: object) -> Generator[Session]:
    """Function-scoped DB session with savepoint rollback for test isolation."""
    connection = postgres_engine.connect()  # type: ignore[union-attr]
    transaction = connection.begin()
    session_factory = sessionmaker(bind=connection)
    session = session_factory()

    # Start a nested savepoint so the test can commit within the session
    session.begin_nested()

    yield session

    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture()
def db_test_user(db_session: Session) -> User:
    """Create a User row in the test database."""
    user = User(
        user_id="test-user-001",
        phone="+15551234567",
        channel_identifier="123456789",
        preferred_channel="telegram",
        onboarding_complete=True,
    )
    db_session.add(user)
    db_session.flush()
    return user


@pytest.fixture()
def db_client(db_session: Session, db_test_user: User) -> Generator[TestClient]:
    """FastAPI test client with DB session and user overrides."""

    def _override_get_db() -> Generator[Session]:
        yield db_session

    def _override_get_current_user() -> User:
        return db_test_user

    webhook_rate_limiter.reset()
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = _override_get_current_user
    with (
        patch("backend.app.main._verify_llm_settings", new_callable=AsyncMock),
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.start"),
        patch("backend.app.channels.telegram.settings.telegram_allowed_chat_ids", "*"),
        patch("backend.app.channels.telegram.settings.telegram_allowed_usernames", ""),
        patch("backend.app.channels.telegram.settings.telegram_bot_token", ""),
        patch("backend.app.agent.ingestion.settings.message_batch_window_ms", 0),
        TestClient(app) as c,
    ):
        yield c
    app.dependency_overrides.clear()
