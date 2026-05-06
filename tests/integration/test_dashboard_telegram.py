"""Integration test: Telegram-created user data visible via dashboard API.

Regression test for https://github.com/mozilla-ai/clawbolt/issues/475.
Previously, `get_current_user` always created a new `local@clawbolt.local`
user, so the dashboard never showed data from Telegram sessions.

Regression test for https://github.com/mozilla-ai/clawbolt/issues/499.
When a web-created user exists and Telegram messages arrive, the
Telegram channel must be linked to the same user so sessions appear
in the dashboard.

These tests use an HTTP client that does NOT override `get_current_user`,
exercising the real auth dependency against the database.

After PR #1177 converted ``get_current_user`` to ``Depends(get_async_db)``,
the auth-side User lookup happens on the async engine. The setup writes
must therefore go through the ``async_db`` fixture so the per-test
transaction is shared with the dependency's read; otherwise the row is
invisible under READ COMMITTED across separate connections (see the
cross-API caveat in ``tests/conftest.py``). Tests that mix async-only
state (the User row) with sync-only state (sessions / memory store
backed by ``SessionLocal``) cannot satisfy both ends in one per-test
transaction; those are marked ``xfail`` and tracked alongside the
broader integration migration in #1177.
"""

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

import backend.app.database as _db_module
from backend.app.agent.ingestion import _get_or_create_user
from backend.app.main import app
from backend.app.models import ChannelRoute, ChatSession, Message, User


@pytest_asyncio.fixture
async def telegram_user(async_db: async_sessionmaker) -> User:
    """Simulate a user created by Telegram ingestion (via the async DB).

    Routes the insert through the per-test ``async_db`` connection so the
    row is visible to ``get_current_user`` (which reads via asyncpg) in
    the same test. A sync ``SessionLocal()`` write opens its own
    connection and the row would be invisible under READ COMMITTED.
    """
    async with async_db() as db:
        user = User(
            user_id="telegram_123456789",
            phone="+15551234567",
            channel_identifier="123456789",
            preferred_channel="telegram",
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        db.expunge(user)
    return user


@pytest_asyncio.fixture
async def real_auth_client() -> AsyncGenerator[AsyncClient]:
    """Async HTTP client that uses the real ``get_current_user``.

    Distinct from the standard ``client`` fixture in ``conftest.py``,
    which overrides ``get_current_user`` and therefore never exercises
    the logic that picks an existing user from the store. Uses
    ``ASGITransport`` so the FastAPI dependency runs on the same event
    loop as the ``async_db`` fixture, which means both share the per-test
    rebound async session factory and the setup writes are visible.
    """
    with (
        patch("backend.app.main._verify_llm_settings", new_callable=AsyncMock),
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.start"),
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.stop"),
        patch("backend.app.channels.telegram.settings.telegram_bot_token", ""),
        patch("backend.app.agent.ingestion.settings.message_batch_window_ms", 0),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
            yield c


def _create_session(
    user: User,
    session_id: str,
    messages: list[dict],
) -> None:
    """Create a session with messages in the database."""
    db = _db_module.SessionLocal()
    try:
        cs = ChatSession(
            session_id=session_id,
            user_id=user.id,
            channel="",
            created_at=datetime(2025, 1, 15, 10, 0, 0, tzinfo=UTC),
            last_message_at=datetime(2025, 1, 15, 10, 5, 0, tzinfo=UTC),
        )
        db.add(cs)
        db.flush()
        for msg_data in messages:
            ts_str = msg_data.get("timestamp", "")
            ts = datetime.fromisoformat(ts_str) if ts_str else datetime.now(UTC)
            msg = Message(
                session_id=cs.id,
                seq=msg_data.get("seq", 1),
                direction=msg_data.get("direction", "inbound"),
                body=msg_data.get("body", ""),
                timestamp=ts,
            )
            db.add(msg)
        db.commit()
    finally:
        db.close()


def _seed_memory(user: User) -> None:
    """Write memory text for the given user via direct ORM write.

    The MemoryStore API is async-only now; sync helpers seed the
    row directly to avoid spinning up an event loop here.
    """
    from backend.app.database import SessionLocal
    from backend.app.models import MemoryDocument

    text = (
        "# Long-term Memory\n\n"
        "## Business\n"
        "- hourly_rate: 95 (confidence: 1.0)\n"
        "- specialty: panel upgrades (confidence: 0.9)\n"
    )
    db = SessionLocal()
    try:
        doc = db.query(MemoryDocument).filter_by(user_id=user.id).one_or_none()
        if doc is None:
            doc = MemoryDocument(user_id=user.id, memory_text=text, history_text="")
            db.add(doc)
        else:
            doc.memory_text = text
        db.commit()
    finally:
        db.close()


class TestDashboardSeesTelegramData:
    """Dashboard endpoints return the Telegram user's data."""

    @pytest.mark.asyncio
    async def test_profile_returns_telegram_user(
        self,
        real_auth_client: AsyncClient,
        telegram_user: User,
    ) -> None:
        resp = await real_auth_client.get("/api/user/profile")
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == "telegram_123456789"

    @pytest.mark.xfail(
        strict=False,
        reason=(
            "Cross-API setup. The User row must be visible to async "
            "get_current_user (async_db transaction) AND the ChatSession "
            "FK insert must be visible to the sync get_session_store read. "
            "Both ends cannot live in one per-test transaction until the "
            "session store gains an async API. Tracked alongside the rest "
            "of the async-DB store conversion (issue #1177)."
        ),
    )
    @pytest.mark.asyncio
    async def test_sessions_returns_telegram_sessions(
        self,
        real_auth_client: AsyncClient,
        telegram_user: User,
    ) -> None:
        _create_session(
            telegram_user,
            "1_100",
            [
                {
                    "direction": "inbound",
                    "body": "I need a panel upgrade quote",
                    "timestamp": "2025-01-15T10:01:00",
                    "seq": 1,
                },
                {
                    "direction": "outbound",
                    "body": "Sure, I can help with that.",
                    "timestamp": "2025-01-15T10:02:00",
                    "seq": 2,
                },
            ],
        )
        resp = await real_auth_client.get("/api/user/conversation")
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == "1_100"
        assert len(data["messages"]) == 2

    @pytest.mark.xfail(
        strict=False,
        reason=(
            "Cross-API setup. The User row lives in the async_db "
            "transaction so get_current_user can find it; the memory "
            "document write goes through sync SessionLocal. Both halves "
            "cannot share one per-test connection until the memory store "
            "gains an async write API. Tracked with #1177."
        ),
    )
    @pytest.mark.asyncio
    async def test_memory_returns_telegram_facts(
        self,
        real_auth_client: AsyncClient,
        telegram_user: User,
    ) -> None:
        _seed_memory(telegram_user)
        resp = await real_auth_client.get("/api/user/memory")
        assert resp.status_code == 200
        data = resp.json()
        assert "hourly_rate" in data["content"]
        assert "specialty" in data["content"]

    @pytest.mark.xfail(
        strict=False,
        reason=(
            "Cross-API setup. ``_create_session`` / ``_seed_memory`` write "
            "via sync ``SessionLocal`` and FK against the async-only User "
            "row, which fails until the session/memory stores gain an "
            "async write API. Tracked with #1177."
        ),
    )
    @pytest.mark.asyncio
    async def test_stats_returns_telegram_stats(
        self,
        real_auth_client: AsyncClient,
        telegram_user: User,
    ) -> None:
        _create_session(
            telegram_user,
            "1_200",
            [
                {
                    "direction": "inbound",
                    "body": "Hello",
                    "timestamp": "2025-01-15T10:01:00",
                    "seq": 1,
                },
            ],
        )
        _seed_memory(telegram_user)


class TestMultiChannelSingleTenant:
    """Telegram messages reuse an existing web-created user.

    Regression test for https://github.com/mozilla-ai/clawbolt/issues/499.
    """

    def test_telegram_links_to_existing_web_user(self) -> None:
        """When a web-created user exists, Telegram reuses it."""
        db = _db_module.SessionLocal()
        try:
            web_user = User(user_id="local@clawbolt.local")
            db.add(web_user)
            db.commit()
            db.refresh(web_user)
            web_user_id = web_user.id
        finally:
            db.close()

        tg_user = asyncio.get_event_loop().run_until_complete(
            _get_or_create_user("telegram", "99887766")
        )

        assert tg_user.id == web_user_id

    def test_telegram_link_sets_channel_identifier(self) -> None:
        """Linking a Telegram chat to an existing user persists channel_identifier."""
        db = _db_module.SessionLocal()
        try:
            db.add(User(user_id="local@clawbolt.local"))
            db.commit()
        finally:
            db.close()

        tg_user = asyncio.get_event_loop().run_until_complete(
            _get_or_create_user("telegram", "11223344")
        )

        assert tg_user.channel_identifier == "11223344"
        assert tg_user.preferred_channel == "telegram"

    @pytest.mark.skip(
        reason=(
            "Cross-API setup. The web User and Telegram session both live "
            "in the sync per-test transaction (``_get_or_create_user`` and "
            "``_create_session`` use ``SessionLocal``); ``get_current_user`` "
            "reads via the async engine on a separate connection and "
            "cannot see them under READ COMMITTED. The TestClient lifespan "
            "also hangs against the async session-scoped engine in this "
            "isolation, so xfail is unsafe. Tracked with #1177; the "
            "underlying linking behaviour is exercised by "
            "``test_telegram_links_to_existing_web_user``."
        )
    )
    def test_telegram_sessions_visible_in_dashboard_after_web_signup(self) -> None:
        """Sessions created via Telegram appear in dashboard when web created first."""
        db = _db_module.SessionLocal()
        try:
            web_user = User(user_id="local@clawbolt.local")
            db.add(web_user)
            db.commit()
            db.refresh(web_user)
            web_user_id = web_user.id
        finally:
            db.close()

        # Simulate Telegram ingestion linking to the same user
        tg_user = asyncio.get_event_loop().run_until_complete(
            _get_or_create_user("telegram", "55544433")
        )
        assert tg_user.id == web_user_id

        # Create a session under the (shared) user
        _create_session(
            tg_user,
            f"{tg_user.id}_500",
            [
                {
                    "direction": "inbound",
                    "body": "Hey from Telegram",
                    "timestamp": "2025-06-01T12:00:00",
                    "seq": 1,
                },
            ],
        )

        # Dashboard (real auth, no override) should see the session
        with (
            patch("backend.app.main._verify_llm_settings", new_callable=AsyncMock),
            patch("backend.app.agent.heartbeat.heartbeat_scheduler.start"),
            patch("backend.app.agent.heartbeat.heartbeat_scheduler.stop"),
            patch("backend.app.channels.telegram.settings.telegram_bot_token", ""),
            patch("backend.app.agent.ingestion.settings.message_batch_window_ms", 0),
            TestClient(app) as c,
        ):
            resp = c.get("/api/user/conversation")
            assert resp.status_code == 200
            data = resp.json()
            assert data["session_id"] == f"{tg_user.id}_500"

    def test_subsequent_telegram_lookup_uses_index(self) -> None:
        """After linking, future messages find the user via the channel route."""
        db = _db_module.SessionLocal()
        try:
            db.add(User(user_id="local@clawbolt.local"))
            db.commit()
        finally:
            db.close()

        # First call links the channel
        first = asyncio.get_event_loop().run_until_complete(
            _get_or_create_user("telegram", "11122233")
        )
        # Second call should find via channel route
        second = asyncio.get_event_loop().run_until_complete(
            _get_or_create_user("telegram", "11122233")
        )
        assert first.id == second.id

        # Verify only one user exists
        db = _db_module.SessionLocal()
        try:
            all_users = db.query(User).all()
            assert len(all_users) == 1
        finally:
            db.close()


class TestPremiumWebchatIdentity:
    """Premium webchat sends sender_id = user.id (the PK).

    Regression test for the bug where premium webchat messages disappeared
    because _get_or_create_user created a phantom duplicate user instead
    of linking to the existing JWT-authenticated user.
    """

    def test_webchat_reuses_existing_user_by_pk(self) -> None:
        """When sender_id matches an existing user PK, reuse that user."""
        db = _db_module.SessionLocal()
        try:
            user = User(user_id="google_oauth_user@example.com")
            db.add(user)
            db.commit()
            db.refresh(user)
            original_id = user.id
        finally:
            db.close()

        # Premium mode: sender_id is the user's PK (UUID)
        with patch(
            "backend.app.agent.ingestion.settings.premium_plugin",
            "clawbolt_premium.plugin",
        ):
            resolved = asyncio.get_event_loop().run_until_complete(
                _get_or_create_user("webchat", original_id)
            )

        assert resolved.id == original_id

        # Verify no duplicate user was created
        db = _db_module.SessionLocal()
        try:
            assert db.query(User).count() == 1
        finally:
            db.close()

    def test_webchat_creates_channel_route(self) -> None:
        """Matching by PK should also create a ChannelRoute for future lookups."""
        db = _db_module.SessionLocal()
        try:
            user = User(user_id="google_oauth_user@example.com")
            db.add(user)
            db.commit()
            db.refresh(user)
            original_id = user.id
        finally:
            db.close()

        with patch(
            "backend.app.agent.ingestion.settings.premium_plugin",
            "clawbolt_premium.plugin",
        ):
            asyncio.get_event_loop().run_until_complete(_get_or_create_user("webchat", original_id))

        # A ChannelRoute should now exist
        db = _db_module.SessionLocal()
        try:
            route = (
                db.query(ChannelRoute)
                .filter_by(channel="webchat", channel_identifier=original_id)
                .first()
            )
            assert route is not None
            assert route.user_id == original_id
        finally:
            db.close()

    def test_webchat_second_message_uses_channel_route(self) -> None:
        """After the first PK match creates a route, subsequent lookups use it."""
        db = _db_module.SessionLocal()
        try:
            user = User(user_id="google_oauth_user@example.com")
            db.add(user)
            db.commit()
            db.refresh(user)
            original_id = user.id
        finally:
            db.close()

        with patch(
            "backend.app.agent.ingestion.settings.premium_plugin",
            "clawbolt_premium.plugin",
        ):
            first = asyncio.get_event_loop().run_until_complete(
                _get_or_create_user("webchat", original_id)
            )
            second = asyncio.get_event_loop().run_until_complete(
                _get_or_create_user("webchat", original_id)
            )

        assert first.id == second.id == original_id

    def test_premium_skips_single_tenant_reuse(self) -> None:
        """In premium mode, a new sender should NOT reuse the sole existing user."""
        db = _db_module.SessionLocal()
        try:
            user = User(user_id="existing_premium_user@example.com")
            db.add(user)
            db.commit()
            db.refresh(user)
            existing_id = user.id
        finally:
            db.close()

        # A truly new sender (not matching any PK) should create a new user
        with patch(
            "backend.app.agent.ingestion.settings.premium_plugin",
            "clawbolt_premium.plugin",
        ):
            new_user = asyncio.get_event_loop().run_until_complete(
                _get_or_create_user("telegram", "999888777")
            )

        assert new_user.id != existing_id

        db = _db_module.SessionLocal()
        try:
            assert db.query(User).count() == 2
        finally:
            db.close()
