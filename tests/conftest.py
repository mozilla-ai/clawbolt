import uuid
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import sessionmaker

import backend.app.database as _db_module
from backend.app.agent.approval import reset_approval_gate
from backend.app.agent.file_store import SessionState, StoredMessage, reset_stores
from backend.app.agent.memory_db import reset_memory_stores
from backend.app.agent.session_db import reset_session_stores
from backend.app.auth.dependencies import get_current_user
from backend.app.bus import message_bus
from backend.app.channels import unknown_sender as unknown_sender_module
from backend.app.config import settings
from backend.app.database import Base
from backend.app.main import app
from backend.app.models import ChatSession, Message, User
from backend.app.services.rate_limiter import webhook_rate_limiter

_TEST_DB_URL = "postgresql+psycopg://clawbolt:clawbolt@localhost:5432/clawbolt_test"
_ASYNC_TEST_DB_URL = "postgresql+asyncpg://clawbolt:clawbolt@localhost:5432/clawbolt_test"


@pytest.fixture(scope="session")
def _pg_engine() -> Generator[Engine]:
    """Session-scoped PostgreSQL engine. Tables are created once per test run."""
    engine = create_engine(_TEST_DB_URL, pool_pre_ping=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest_asyncio.fixture
async def _pg_async_engine(_pg_engine: Engine) -> AsyncGenerator[AsyncEngine]:
    """Function-scoped async PostgreSQL engine.

    Depends on ``_pg_engine`` so the sync engine fixture has already
    created (and will later drop) the schema. The async engine only
    opens connections; it never runs DDL. Both engines target the
    same database; each maintains its own connection pool, mirroring
    the production setup in ``backend.app.database``.

    Scope is per-test rather than per-session because asyncpg
    connections bind to the event loop they were created on, and
    pytest-asyncio runs each test on a fresh function-scoped loop by
    default. A session-scoped engine would surface as
    ``RuntimeError: Future attached to a different loop`` on the
    second test. We pay one engine setup per async test (a few ms)
    in exchange for not having to widen the loop scope across the
    whole suite, which would entangle sync and async tests.
    """
    engine = create_async_engine(_ASYNC_TEST_DB_URL, pool_pre_ping=True)
    yield engine
    await engine.dispose()


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


# ---------------------------------------------------------------------------
# Async DB isolation fixture (issue #1148)
# ---------------------------------------------------------------------------
#
# Pattern: per-test ``AsyncConnection`` + outer transaction, with the
# module-level ``_async_session_factory`` rebound to an
# ``async_sessionmaker(bind=connection, join_transaction_mode=
# "create_savepoint")`` for the duration of the test. The async store
# API (``IdempotencyStore.try_mark_seen_async`` etc.) calls
# ``AsyncSessionLocal()``/``db_session_async()`` -> picks up the
# rebound factory -> shares the per-test connection. Each
# ``factory()``/``db_session_async()`` opens a new SAVEPOINT under the
# outer transaction; ``await session.commit()`` releases that
# SAVEPOINT only; ``await session.rollback()`` (e.g. on
# ``IntegrityError``) unwinds to the SAVEPOINT and leaves the outer
# transaction intact. The outer ``await connection.begin()``
# transaction is rolled back at teardown, leaving a clean DB
# regardless of how many awaits or commit/rollback cycles the test
# performed.
#
# Differences from the sync ``_isolate_stores`` analog above:
#   * Driver: asyncpg vs psycopg (sync psycopg3). The async engine is
#     built from ``postgresql+asyncpg://`` and lives on a
#     function-scoped ``_pg_async_engine`` fixture (asyncpg connections
#     bind to the event loop they were created on, so a session-scoped
#     engine dies when pytest-asyncio rotates loops between tests).
#   * Join mode: ``create_savepoint``, not ``conditional_savepoint``.
#     The sync version's ``conditional_savepoint`` survives an
#     ``IntegrityError`` because the sync psycopg driver keeps the
#     outer transaction alive when only the savepoint aborts. The
#     asyncpg path detaches
#     the outer transaction in the same scenario, which would surface
#     to tests as "the row I just committed disappeared after a
#     duplicate-insert error in a later session". Forcing every
#     session into its own SAVEPOINT (``create_savepoint``) keeps the
#     contract consistent across drivers.
#   * Scope: opt-in. Sync tests do not request this fixture, so they
#     pay no async setup cost. Tests that exercise the async store API
#     add ``async_db`` to their parameter list.
#   * Cross-API caveat: the sync and async per-test transactions live
#     on independent connections. A sync write committed from
#     ``_isolate_stores`` is NOT visible to an async read in the same
#     test (each transaction sees its own snapshot under READ
#     COMMITTED). Pure-async store tests are fine; mixed-API tests
#     should drive their setup through the matching API.
#
# Future store-conversion PRs (#1151, #1152, #1153, #1154, #1155,
# #1156, #1157, #1175) and the premium analog (premium #390) should
# mirror this pattern: one ``AsyncConnection``, one outer transaction,
# ``create_savepoint`` mode, rebind the factory, rollback at teardown.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def async_db(
    _pg_async_engine: AsyncEngine,
) -> AsyncGenerator[async_sessionmaker]:
    """Per-test async DB isolation via SAVEPOINT-on-connection rollback.

    Opt-in: request ``async_db`` from any test that needs the async
    store API to run inside a per-test transaction. The fixture
    rebinds ``backend.app.database._async_session_factory`` (and
    ``_async_engine`` for completeness) so calls to
    ``AsyncSessionLocal()`` / ``db_session_async()`` pick up the
    test-scoped factory. See the design comment block above for the
    full rationale and the pattern future store tests should mirror.
    """
    connection = await _pg_async_engine.connect()
    transaction = await connection.begin()

    test_async_factory: async_sessionmaker = async_sessionmaker(
        bind=connection,
        autoflush=False,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )

    old_async_engine = _db_module._async_engine
    old_async_factory = _db_module._async_session_factory
    _db_module._async_engine = _pg_async_engine
    _db_module._async_session_factory = test_async_factory

    try:
        yield test_async_factory
    finally:
        # Rollback unwinds the outer transaction; any inner SAVEPOINTs
        # the test left open go with it. Mirrors the sync fixture's
        # ``is_active`` guard: an unrecovered error inside the test
        # may have already detached the transaction.
        if transaction.is_active:
            await transaction.rollback()
        await connection.close()
        _db_module._async_engine = old_async_engine
        _db_module._async_session_factory = old_async_factory


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


@pytest_asyncio.fixture
async def async_test_user(async_db: async_sessionmaker) -> User:
    """Insert a User row through the async per-test transaction.

    Async peer of the sync ``test_user`` fixture above. Routes the
    write through the async connection so the row is visible to async
    store reads in the same test. The sync ``test_user`` fixture opens
    its own per-test transaction on a separate connection; rows
    committed there are invisible to the async store under READ
    COMMITTED, which is the cross-API caveat called out in the design
    comment block above.

    The async fixture's outer transaction rollback unwinds the insert
    at teardown, so no explicit cleanup is needed. Shared by store
    test files exercising the dual-API (#1153, #1151, #1152, #1154,
    #1155, #1156, #1157, #1175).
    """
    async with async_db() as db:
        user = User(
            id=str(uuid.uuid4()),
            user_id="async-test-user",
            phone="+15555550123",
            channel_identifier="async-test-channel",
            preferred_channel="telegram",
            onboarding_complete=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        # Detach so attribute access after the session closes does not
        # trigger lazy IO.
        db.expunge(user)
    return user


def create_test_session(
    user_id: str,
    session_id: str = "test-conv",
    messages: list[StoredMessage] | None = None,
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
            channel=channel,
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


@pytest.fixture(autouse=True)
def _stub_unknown_sender_reply() -> Generator[AsyncMock]:
    """Patch the unknown-sender reply at the import site in ``base.py``.

    Without this, every existing allowlist-rejection test would trigger a real
    outbound HTTP call (Linq/BlueBubbles/Telegram) and hang on the configured
    timeout. Tests that exercise the unknown-sender behavior import
    ``reply_to_unknown_sender`` directly from its module, bypassing this patch.
    """
    unknown_sender_module.reset_unknown_sender_cache()
    with patch(
        "backend.app.channels.base.reply_to_unknown_sender", new_callable=AsyncMock
    ) as mock_reply:
        yield mock_reply
    unknown_sender_module.reset_unknown_sender_cache()


@pytest.fixture()
def linq_client(test_user: User) -> Generator[TestClient]:
    """FastAPI test client with Linq channel available.

    The Linq channel is always registered at module level in main.py.
    This fixture patches settings to allow all numbers and disable HMAC.
    """

    def _override_get_current_user() -> User:
        return test_user

    webhook_rate_limiter.reset()
    app.dependency_overrides[get_current_user] = _override_get_current_user

    # Reset the Linq channel's chat cache between tests
    from backend.app.channels import get_channel
    from backend.app.channels.linq import LinqChannel

    channel = get_channel("linq")
    if isinstance(channel, LinqChannel):
        channel._chat_cache.clear()

    with (
        patch("backend.app.main._verify_llm_settings", new_callable=AsyncMock),
        patch("backend.app.main._enforce_single_channel"),
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.start"),
        patch("backend.app.channels.linq.settings.linq_allowed_numbers", "*"),
        patch("backend.app.channels.linq.settings.linq_webhook_signing_secret", ""),
        patch("backend.app.channels.telegram.settings.telegram_allowed_chat_id", "*"),
        patch("backend.app.channels.telegram.settings.telegram_bot_token", ""),
        patch("backend.app.agent.ingestion.settings.message_batch_window_ms", 0),
        TestClient(app) as c,
    ):
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def bluebubbles_client(test_user: User) -> Generator[TestClient]:
    """FastAPI test client with BlueBubbles channel available.

    Patches settings to allow all numbers and disable password validation.
    """

    def _override_get_current_user() -> User:
        return test_user

    webhook_rate_limiter.reset()
    app.dependency_overrides[get_current_user] = _override_get_current_user

    # Reset the BlueBubbles channel's chat cache between tests
    from backend.app.channels import get_channel
    from backend.app.channels.bluebubbles import BlueBubblesChannel

    channel = get_channel("bluebubbles")
    if isinstance(channel, BlueBubblesChannel):
        channel._chat_cache.clear()

    with (
        patch("backend.app.main._verify_llm_settings", new_callable=AsyncMock),
        patch("backend.app.main._enforce_single_channel"),
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.start"),
        patch("backend.app.channels.bluebubbles.settings.bluebubbles_allowed_numbers", "*"),
        patch("backend.app.channels.bluebubbles.settings.bluebubbles_password", ""),
        patch("backend.app.channels.telegram.settings.telegram_allowed_chat_id", "*"),
        patch("backend.app.channels.telegram.settings.telegram_bot_token", ""),
        patch("backend.app.channels.linq.settings.linq_allowed_numbers", "*"),
        patch("backend.app.channels.linq.settings.linq_webhook_signing_secret", ""),
        patch("backend.app.agent.ingestion.settings.message_batch_window_ms", 0),
        TestClient(app) as c,
    ):
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def client(test_user: User) -> Generator[TestClient]:
    """FastAPI test client with overridden auth."""

    def _override_get_current_user() -> User:
        return test_user

    webhook_rate_limiter.reset()
    app.dependency_overrides[get_current_user] = _override_get_current_user
    with (
        patch("backend.app.main._verify_llm_settings", new_callable=AsyncMock),
        patch("backend.app.main._enforce_single_channel"),
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.start"),
        # Default allowlist to "*" (allow all) so tests are not blocked.
        # Individual allowlist tests override these values.
        patch("backend.app.channels.telegram.settings.telegram_allowed_chat_id", "*"),
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
