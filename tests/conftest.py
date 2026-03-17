import uuid
from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import sessionmaker

import backend.app.database as _db_module
from backend.app.agent.approval import reset_approval_gate
from backend.app.agent.file_store import SessionState, StoredMessage, reset_stores
from backend.app.agent.memory_db import reset_memory_stores
from backend.app.agent.session_db import reset_session_stores
from backend.app.auth.dependencies import get_current_user
from backend.app.bus import message_bus
from backend.app.config import settings
from backend.app.database import Base
from backend.app.main import app
from backend.app.models import ChatSession, Message, User
from backend.app.services.rate_limiter import webhook_rate_limiter

_TEST_DB_URL = "postgresql://clawbolt:clawbolt@localhost:5432/clawbolt_test"


@pytest.fixture(scope="session")
def _pg_engine() -> Generator[Engine]:
    """Session-scoped PostgreSQL engine. Tables are created once per test run."""
    engine = create_engine(_TEST_DB_URL, pool_pre_ping=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture(autouse=True)
def _isolate_stores(_pg_engine: Engine, tmp_path: Path) -> Generator[None]:
    """Per-test isolation using PostgreSQL with transaction rollback.

    Opens a connection, begins a transaction, and binds the session factory
    to it with join_transaction_block=True. Store code calls SessionLocal()
    and commit() normally, but commits only affect a subtransaction. After
    the test, the outer transaction is rolled back, leaving a clean DB.
    """
    connection = _pg_engine.connect()
    transaction = connection.begin()

    test_session_factory = sessionmaker(
        autocommit=False,
        autoflush=False,
        bind=connection,
        join_transaction_mode="conditional_savepoint",
    )

    old_engine = _db_module._engine
    old_factory = _db_module._SessionLocal

    _db_module._engine = _pg_engine
    _db_module._SessionLocal = test_session_factory

    # Set up per-test file store isolation
    with patch.object(settings, "data_dir", str(tmp_path)):
        reset_stores()
        reset_session_stores()
        reset_memory_stores()
        reset_approval_gate()
        yield

    # Rollback undoes all data written during the test.
    # The transaction may already be deassociated if a test triggered
    # an IntegrityError (e.g. unique constraint tests), so check first.
    if transaction.is_active:
        transaction.rollback()
    connection.close()

    # Restore
    _db_module._engine = old_engine
    _db_module._SessionLocal = old_factory
    reset_stores()
    reset_session_stores()
    reset_memory_stores()
    reset_approval_gate()


@pytest.fixture()
async def test_user(tmp_path: Path) -> User:
    """Create a test user in the per-test PostgreSQL transaction.

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
