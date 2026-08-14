"""Endpoint tests for /admin/shared-data (issue #325 item 3).

Covers:
- The consent gate: non-consenting users return 403, consenting ones
  return 200 with their content.
- PII redaction: planted phone / email / card markers in message
  bodies do NOT appear in the wire response.
- Audit logging: every read writes one ``AdminAuditLog`` row.

Async DB conversion (issue #393, #429): the router runs on
``Depends(get_async_db)`` and ``get_current_admin`` now also resolves
through the async session, so the admin's User + Subscription must be
visible to the per-test ASYNC connection. The ``audit_admin._try_commit``
insert still runs sync (fresh ``SessionLocal()``) and FK-references
``admin_audit_logs.admin_user_id`` -> ``users.id`` and
``target_user_id`` -> ``users.id``, so the same rows must be visible
to the sync connection too. Both bridges go through autocommit:
``_autocommit_admin`` seeds the admin caller, ``_consenting_user``
seeds the target users; rows commit outside both per-test transactions
and both connections see them under READ COMMITTED.

The two per-test connections (sync ``_isolate_stores`` and async
``async_db``) are disjoint under READ COMMITTED. To bridge that, the
``_consenting_user`` helper writes through a DIRECT engine connection
that commits to the real DB outside both per-test transactions. Both
transactions then read the row through the standard READ COMMITTED
visibility rules. The rows leak between tests but UUID ids prevent
cross-test conflicts; the session-scoped ``_pg_engine`` fixture drops
all tables at session teardown, sweeping the leaks.
"""

from __future__ import annotations

import datetime as _dt
import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import Session

from backend.app.agent.approval import ApprovalEventRecord
from backend.app.middleware.rate_limit import _auth_rate_limiter
from backend.app.models import AdminAuditLog, ChatSession, Message, Subscription, User
from tests.multi_user.conftest import get_test_sync_engine

_SHARED_USER_IDS: list[str] = []


def _sweep_shared_users() -> None:
    """DELETE every User row leaked by ``_shared_user`` so far.

    Runs at the START of each autouse setup, AFTER prior tests'
    per-test transactions have already rolled back. Doing the sweep
    BEFORE setup avoids deadlocking on FK row locks held by an
    in-progress per-test transaction (under READ COMMITTED, a parent
    DELETE blocks until every child txn ends).

    Builds a fresh engine against the same URL the conftest's
    ``_pg_engine`` fixture uses rather than going through the OSS
    module-level ``_engine`` slot: ``_isolate_stores`` swaps that slot
    in and out, and a module-scoped finalizer firing between tests
    can hit a moment where the slot points at the wrong generation.
    """
    from sqlalchemy import create_engine, delete

    from tests.multi_user.conftest import _TEST_DB_URL

    if not _SHARED_USER_IDS:
        return
    engine = create_engine(_TEST_DB_URL)
    try:
        conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        try:
            with Session(bind=conn) as s:
                # FK CASCADE on approval_events / sessions / messages / etc.
                # carries dependent rows away when the User goes;
                # Subscription needs an explicit delete first.
                s.execute(delete(Subscription).where(Subscription.user_id.in_(_SHARED_USER_IDS)))
                s.execute(delete(User).where(User.id.in_(_SHARED_USER_IDS)))
                s.commit()
        finally:
            conn.close()
    finally:
        engine.dispose()
        _SHARED_USER_IDS.clear()


@pytest.fixture(autouse=True)
def _clear_rate_limits() -> None:
    _sweep_shared_users()
    _auth_rate_limiter.reset()


@pytest.fixture(scope="module", autouse=True)
def _sweep_at_module_exit() -> Generator[None]:
    """Final sweep after the module's tests finish.

    The per-test ``_clear_rate_limits`` sweeps at the START of each
    test, which keeps within-module state bounded but does NOT run
    after the LAST test ends. Module-scoped autouse + finalizer
    ensures leaked rows do not survive into other test modules
    (e.g. global user-population assertions in
    ``test_heartbeat_delegation``).
    """
    yield
    _sweep_shared_users()


def _autocommit_admin() -> User:
    """Insert an admin User + Subscription via a direct autocommit connection.

    After #429 ``get_current_admin`` reads the admin's Subscription
    through ``get_async_db`` (async per-test connection), while the
    ``audit_admin._try_commit`` insert FK-references the admin's User
    row through sync ``SessionLocal``. The two per-test connections
    (sync ``_isolate_stores`` and async ``async_db``) are disjoint
    under READ COMMITTED, so a row written through either fixture is
    invisible to the other side. Committing the admin row through a
    direct autocommit connection (mirroring ``_shared_user`` above)
    makes both rows visible to both per-test transactions.

    Tracked in ``_SHARED_USER_IDS`` so the per-module sweep cleans
    them up at module exit.
    """
    user = User(
        id=str(uuid.uuid4()),
        user_id=f"google_admin_{uuid.uuid4().hex[:8]}",
        phone="",
        onboarding_complete=True,
    )
    sub = Subscription(
        user_id=user.id,
        role="admin",
        plan="free",
        status="active",
        email="admin@test.example",
    )
    engine = get_test_sync_engine()
    conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        with Session(bind=conn) as s:
            # Flush the User before adding the Subscription. Neither model
            # declares an ORM relationship to the other, so a single flush
            # orders the two INSERTs by mapper sort key, which puts
            # subscriptions first and violates the FK.
            s.add(user)
            s.flush()
            s.add(sub)
            s.commit()
            s.refresh(user)
            s.expunge(user)
    finally:
        conn.close()
    _SHARED_USER_IDS.append(user.id)
    return user


@pytest_asyncio.fixture
async def async_client(
    async_db: async_sessionmaker,
) -> AsyncGenerator[httpx.AsyncClient]:
    """ASGI-driven async HTTP client wired to an admin caller.

    After #429 ``get_current_admin`` resolves through ``get_async_db``,
    so the admin's Subscription must be visible to the async per-test
    connection. The sync ``test_subscription`` fixture writes through
    ``db_session`` only; under READ COMMITTED the async connection
    cannot see that row. The fix is symmetric with how
    ``_consenting_user`` seeds target rows: write the admin User +
    Subscription through a direct autocommit connection so both the
    async ``get_current_admin`` lookup AND the sync
    ``audit_admin._try_commit`` FK insert see the same row. Route data
    still lives on ``async_db``'s separate connection; cross-API setup
    is documented in the module docstring.
    """
    from backend.app.auth.dependencies import get_current_user
    from tests.multi_user.conftest import MULTI_USER_APP as app

    admin_user = _autocommit_admin()
    app.dependency_overrides[get_current_user] = lambda: admin_user
    mock_manager = MagicMock()
    mock_manager.start_all = AsyncMock(return_value=[])
    mock_manager.stop_all = AsyncMock()
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
        patch("backend.app.main.heartbeat_scheduler"),
        patch("backend.app.main.oauth_refresh_scheduler"),
        patch("backend.app.main.get_manager", return_value=mock_manager),
    ):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client

    app.dependency_overrides.pop(get_current_user, None)


def _shared_user(
    *,
    consent: bool,
    email: str = "consent@example.com",
    soul_text: str = "",
    user_text: str = "",
    heartbeat_text: str = "",
    heartbeat_opt_in: bool = True,
    heartbeat_frequency: str = "30m",
    heartbeat_max_daily: int = 5,
) -> User:
    """Insert a User + Subscription via a direct autocommit connection.

    Bypasses both the sync ``_isolate_stores`` and async ``async_db``
    per-test transactions: the row commits to the underlying database
    so both per-test transactions can read it under READ COMMITTED.
    Without this, the route's async consent gate would see the row
    only via the async path while the sync ``audit_admin._try_commit``
    insert would fail the FK on ``admin_audit_logs.target_user_id``.

    Rows accumulate during a pytest session (no rollback at end-of-test
    since they live outside the per-test transaction); each test mints
    a fresh UUID so collisions cannot happen, and the session-scoped
    ``_pg_engine`` drops every table at teardown.
    """
    user = User(
        id=str(uuid.uuid4()),
        user_id=f"google_{uuid.uuid4().hex[:8]}",
        phone="",
        onboarding_complete=True,
        data_sharing_consent=consent,
        data_sharing_consent_at=_dt.datetime.now(_dt.UTC) if consent else None,
        soul_text=soul_text,
        user_text=user_text,
        heartbeat_text=heartbeat_text,
        heartbeat_opt_in=heartbeat_opt_in,
        heartbeat_frequency=heartbeat_frequency,
        heartbeat_max_daily=heartbeat_max_daily,
    )
    sub = Subscription(user_id=user.id, role="user", plan="free", status="active", email=email)
    engine = get_test_sync_engine()
    conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        with Session(bind=conn) as s:
            # Flush the User before adding the Subscription. Neither model
            # declares an ORM relationship to the other, so a single flush
            # orders the two INSERTs by mapper sort key, which puts
            # subscriptions first and violates the FK.
            s.add(user)
            s.flush()
            s.add(sub)
            s.commit()
            s.refresh(user)
            s.expunge(user)
    finally:
        conn.close()
    _SHARED_USER_IDS.append(user.id)
    return user


async def _consenting_user(
    async_db: async_sessionmaker,
    *,
    email: str = "consent@example.com",
) -> User:
    """Create a consenting User visible to both sync and async paths."""
    return _shared_user(consent=True, email=email)


async def _non_consenting_user(async_db: async_sessionmaker) -> User:
    """Create a non-consenting User visible to both sync and async paths."""
    return _shared_user(consent=False, email="silent@example.com")


async def _flip_consent(async_db: async_sessionmaker, user_id: str, *, consent: bool) -> None:
    """Toggle ``data_sharing_consent`` on an existing user.

    Writes via a direct autocommit connection so the change is visible
    to both per-test transactions, matching how ``_shared_user`` seeds
    the original row.
    """
    from sqlalchemy import update

    engine = get_test_sync_engine()
    conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        with Session(bind=conn) as s:
            s.execute(
                update(User)
                .where(User.id == user_id)
                .values(
                    data_sharing_consent=consent,
                    data_sharing_consent_at=_dt.datetime.now(_dt.UTC),
                )
            )
            s.commit()
    finally:
        conn.close()


class TestSharedDataSummary:
    async def test_summary_zero_when_no_consenting_users(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """With no consenting users, every count is zero."""
        # test_user is the admin; ensure the admin's User row in async_db
        # is also non-consenting so the summary stays at zero. The admin
        # row was created via the sync test_user fixture; under READ
        # COMMITTED the async connection cannot see it, so there is
        # nothing to flip and the count is naturally zero.
        resp = await async_client.get("/api/admin/shared-data/summary")
        assert resp.status_code == 200
        body = resp.json()
        assert body["consenting_user_count"] == 0
        assert body["consents_changed_this_week"] == 0
        assert body["conversations_this_week"] == 0
        assert body["heartbeats_this_week"] == 0
        assert body["top_users_this_week"] == []
        # Sanity: a heartbeat-error metric is intentionally absent (the
        # OSS scheduler does not emit ``action_type="error"``).
        assert "error_heartbeats_this_week" not in body

    async def test_summary_counts_consenting_users_and_recent_activity(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """Counts reflect consenting users and last-7-day activity."""
        from backend.app.models import HeartbeatLog

        consenter = await _consenting_user(async_db, email="active@example.com")
        # A second consenter whose conversation is too old to count toward
        # this_week (sessions are 1:1 with users, so we can't put two
        # sessions on a single user).
        old_consenter = await _consenting_user(async_db, email="dormant@example.com")

        async with async_db() as db:
            sess_recent = ChatSession(
                user_id=consenter.id,
                session_id=f"sess-{uuid.uuid4().hex[:8]}",
                channel="imessage",
                last_message_at=_dt.datetime.now(_dt.UTC),
            )
            sess_old = ChatSession(
                user_id=old_consenter.id,
                session_id=f"sess-{uuid.uuid4().hex[:8]}",
                channel="imessage",
                last_message_at=_dt.datetime.now(_dt.UTC) - _dt.timedelta(days=30),
            )
            db.add_all([sess_recent, sess_old])
            await db.flush()

            # Recent heartbeats with the action types the OSS scheduler
            # actually emits (send | skip | cleanup).
            db.add_all(
                [
                    HeartbeatLog(
                        user_id=consenter.id,
                        action_type="send",
                        channel="imessage",
                        created_at=_dt.datetime.now(_dt.UTC),
                    ),
                    HeartbeatLog(
                        user_id=consenter.id,
                        action_type="cleanup",
                        channel="imessage",
                        created_at=_dt.datetime.now(_dt.UTC),
                    ),
                ]
            )
            # Recent message
            db.add(
                Message(
                    session_id=sess_recent.id,
                    seq=1,
                    direction="inbound",
                    body="hello",
                    timestamp=_dt.datetime.now(_dt.UTC),
                )
            )
            await db.commit()

        resp = await async_client.get("/api/admin/shared-data/summary")
        assert resp.status_code == 200
        body = resp.json()
        assert body["consenting_user_count"] >= 1
        # ``_consenting_user`` stamps consent_at to now(), so the user
        # qualifies as a recent consent toggle.
        assert body["consents_changed_this_week"] >= 1
        assert body["conversations_this_week"] >= 1
        assert body["heartbeats_this_week"] >= 2
        # Top user leaderboard surfaces the active consenter.
        top = body["top_users_this_week"]
        assert any(t["id"] == consenter.id and t["messages_this_week"] >= 1 for t in top)

    async def test_summary_includes_open_reports_count(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """open_reports_count counts dismissed_at IS NULL across all users."""
        from backend.app.models import ReportedConversation

        consenter = await _consenting_user(async_db, email="reporter@example.com")
        async with async_db() as db:
            sess = ChatSession(
                user_id=consenter.id,
                session_id=f"sess-{uuid.uuid4().hex[:8]}",
                channel="imessage",
            )
            db.add(sess)
            await db.flush()
            # One open, one dismissed
            db.add_all(
                [
                    ReportedConversation(
                        user_id=consenter.id,
                        session_id=sess.id,
                        reason="bad bot",
                    ),
                    ReportedConversation(
                        user_id=consenter.id,
                        session_id=sess.id,
                        reason="also bad",
                        dismissed_at=_dt.datetime.now(_dt.UTC),
                    ),
                ]
            )
            await db.commit()

        resp = await async_client.get("/api/admin/shared-data/summary")
        assert resp.status_code == 200
        body = resp.json()
        assert body["open_reports_count"] >= 1

    async def test_summary_writes_audit_row(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
        db_session: Session,
    ) -> None:
        """Each summary read writes one audit row."""
        before = db_session.query(AdminAuditLog).count()
        resp = await async_client.get("/api/admin/shared-data/summary")
        assert resp.status_code == 200
        after = db_session.query(AdminAuditLog).count()
        assert after == before + 1


class TestSharedDataUserList:
    async def test_lists_only_consenting_users(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """The list endpoint must filter out users who haven't consented."""
        consenter = await _consenting_user(async_db, email="opted-in@example.com")
        await _non_consenting_user(async_db)

        resp = await async_client.get("/api/admin/shared-data/users")
        assert resp.status_code == 200
        data = resp.json()
        emails = [u["email"] for u in data["items"]]
        assert "opted-in@example.com" in emails
        assert "silent@example.com" not in emails
        # Sanity: the consenting user item carries metadata for the UI.
        item = next(u for u in data["items"] if u["id"] == consenter.id)
        assert item["email"] == "opted-in@example.com"
        assert item["consent_at"] is not None
        assert item["conversation_count"] == 0
        assert item["last_message_at"] is None

    async def test_includes_the_latest_message_time(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """The shared-user list exposes a consenting user's latest activity."""
        consenter = await _consenting_user(async_db, email="recent@example.com")
        latest = _dt.datetime(2026, 4, 21, 12, 0, tzinfo=_dt.UTC)

        async with async_db() as db:
            db.add(
                ChatSession(
                    user_id=consenter.id,
                    session_id=f"latest-{uuid.uuid4().hex[:8]}",
                    channel="imessage",
                    last_message_at=latest,
                )
            )
            await db.commit()

        resp = await async_client.get("/api/admin/shared-data/users")
        assert resp.status_code == 200
        item = next(user for user in resp.json()["items"] if user["id"] == consenter.id)
        assert item["conversation_count"] == 1
        assert item["last_message_at"].startswith("2026-04-21T12:00:00")

    async def test_writes_audit_row_per_call(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
        db_session: Session,
    ) -> None:
        await _consenting_user(async_db)

        before = db_session.query(AdminAuditLog).count()
        resp = await async_client.get("/api/admin/shared-data/users")
        assert resp.status_code == 200
        after = db_session.query(AdminAuditLog).count()
        assert after == before + 1
        latest = db_session.query(AdminAuditLog).order_by(AdminAuditLog.id.desc()).first()
        assert latest is not None
        assert latest.action == "view_shared_data_users"


class TestSharedDataConversationForUser:
    async def test_returns_403_for_non_consenting_user(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        non_consenter = await _non_consenting_user(async_db)
        resp = await async_client.get(
            f"/api/admin/shared-data/users/{non_consenter.id}/conversation"
        )
        assert resp.status_code == 403
        assert "consented" in resp.json()["detail"].lower()

    async def test_returns_404_for_unknown_user(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        resp = await async_client.get(f"/api/admin/shared-data/users/{uuid.uuid4()}/conversation")
        assert resp.status_code == 404

    async def test_returns_404_when_user_has_no_conversation_yet(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """Consenting user without a session row returns 404, not an empty body."""
        consenter = await _consenting_user(async_db)
        resp = await async_client.get(f"/api/admin/shared-data/users/{consenter.id}/conversation")
        assert resp.status_code == 404

    async def test_returns_conversation_for_consenting_user(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        consenter = await _consenting_user(async_db)
        async with async_db() as db:
            cs = ChatSession(
                session_id=f"sess-{uuid.uuid4().hex[:8]}",
                user_id=consenter.id,
                channel="imessage",
            )
            db.add(cs)
            await db.flush()
            db.add(Message(session_id=cs.id, seq=1, direction="inbound", body="hello"))
            await db.commit()
            session_id_str = cs.session_id

        resp = await async_client.get(f"/api/admin/shared-data/users/{consenter.id}/conversation")
        assert resp.status_code == 200
        data = resp.json()
        assert data["channel"] == "imessage"
        assert data["message_count"] == 1
        assert data["session_id"] == session_id_str
        # Untrimmed sessions surface last_trim_seq=None so the UI can
        # skip the "trimmed by compaction" boundary line entirely.
        assert data["last_trim_seq"] is None

    async def test_includes_last_trim_seq_when_session_has_been_trimmed(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """The agent's trim path advances ``sessions.last_trim_seq`` to
        the highest dropped ``messages.seq``. The admin endpoint surfaces
        this so the timeline can render messages with seq <=
        last_trim_seq greyed-out as "trimmed by compaction."
        """
        consenter = await _consenting_user(async_db)
        async with async_db() as db:
            cs = ChatSession(
                session_id=f"sess-{uuid.uuid4().hex[:8]}",
                user_id=consenter.id,
                channel="imessage",
                last_trim_seq=42,
            )
            db.add(cs)
            await db.commit()

        resp = await async_client.get(f"/api/admin/shared-data/users/{consenter.id}/conversation")
        assert resp.status_code == 200
        assert resp.json()["last_trim_seq"] == 42

    async def test_turns_endpoint_includes_last_trim_seq(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """The turn-grouped endpoint also carries the watermark so the
        timeline view, which is the canonical conversation render, can
        place the boundary line between trimmed and live turns without
        a second roundtrip.
        """
        consenter = await _consenting_user(async_db)
        async with async_db() as db:
            cs = ChatSession(
                session_id=f"sess-{uuid.uuid4().hex[:8]}",
                user_id=consenter.id,
                channel="imessage",
                last_trim_seq=7,
            )
            db.add(cs)
            await db.flush()
            db.add(Message(session_id=cs.id, seq=8, direction="inbound", body="hi"))
            await db.commit()

        resp = await async_client.get(
            f"/api/admin/shared-data/users/{consenter.id}/conversation/turns"
        )
        assert resp.status_code == 200
        assert resp.json()["last_trim_seq"] == 7


async def _make_session_with_tools(async_db: async_sessionmaker, consenter: User) -> ChatSession:
    """Build a fixture session: one inbound, one outbound w/ 2 tools.

    Mirrors the shape of a real agent turn: user asks something,
    agent replies and the reply message carries
    ``tool_interactions_json`` listing the tools the agent fired
    before composing the reply.
    """
    async with async_db() as db:
        cs = ChatSession(
            session_id=f"sess-{uuid.uuid4().hex[:8]}",
            user_id=consenter.id,
            channel="imessage",
        )
        db.add(cs)
        await db.flush()
        tool_payload = (
            '[{"tool_call_id":"call_1","name":"qb_query",'
            '"args":{"query":"SELECT * WHERE customer_name = '
            "'John Smith'\"},"
            '"result":"Found row for John Smith with phone +15555550123",'
            '"is_error":false},'
            '{"tool_call_id":"call_2","name":"companycam_search_projects",'
            '"args":{"query":"kitchen remodel"},'
            '"result":"3 projects matched",'
            '"is_error":true}]'
        )
        db.add_all(
            [
                Message(
                    session_id=cs.id,
                    seq=1,
                    direction="inbound",
                    body="show me my pending estimates",
                ),
                Message(
                    session_id=cs.id,
                    seq=2,
                    direction="outbound",
                    body="Here are the 4 pending estimates I found",
                    tool_interactions_json=tool_payload,
                ),
            ]
        )
        await db.commit()
        await db.refresh(cs)
        db.expunge(cs)
    return cs


class TestSharedDataConversationTurns:
    """Tests for the /turns endpoint that surfaces tool calls.

    Turn grouping pairs each inbound user message with the outbound
    agent reply(ies) that follow. Tool-call args / results are
    redacted at the leaves (see _redact_tool_call) so customer names
    and other third-party PII inside tool arguments do not leak.
    """

    async def test_403_for_non_consenting_user(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """Consent gate must block the /turns endpoint."""
        silent = await _non_consenting_user(async_db)
        async with async_db() as db:
            cs = ChatSession(
                session_id=f"sess-{uuid.uuid4().hex[:8]}",
                user_id=silent.id,
                channel="imessage",
            )
            db.add(cs)
            await db.flush()
            db.add(Message(session_id=cs.id, seq=1, direction="inbound", body="hello"))
            await db.commit()

        resp = await async_client.get(
            f"/api/admin/shared-data/users/{silent.id}/conversation/turns"
        )
        assert resp.status_code == 403

    async def test_returns_404_for_unknown_user(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """Unknown user id returns 404, not a leak."""
        resp = await async_client.get(
            f"/api/admin/shared-data/users/{uuid.uuid4()}/conversation/turns"
        )
        assert resp.status_code == 404

    async def test_returns_404_when_user_has_no_conversation_yet(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """Consenting user without a session row returns 404."""
        consenter = await _consenting_user(async_db)
        resp = await async_client.get(
            f"/api/admin/shared-data/users/{consenter.id}/conversation/turns"
        )
        assert resp.status_code == 404

    async def test_returns_403_when_user_revokes_consent_after_session_existed(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """Load-bearing guarantee: revoked consent must block reads even
        for conversations that already existed when consent was granted.
        Without the consent re-check on the per-session lookup, an
        admin who started reading could keep reading after the user
        opted out.
        """
        consenter = await _consenting_user(async_db)
        async with async_db() as db:
            cs = ChatSession(
                session_id=f"sess-{uuid.uuid4().hex[:8]}",
                user_id=consenter.id,
                channel="imessage",
            )
            db.add(cs)
            await db.flush()
            db.add(Message(session_id=cs.id, seq=1, direction="inbound", body="hi"))
            await db.commit()

        await _flip_consent(async_db, consenter.id, consent=False)

        resp = await async_client.get(
            f"/api/admin/shared-data/users/{consenter.id}/conversation/turns"
        )
        assert resp.status_code == 403

    async def test_redacts_pii_in_user_message_and_agent_reply_bodies(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """PII redaction applies to user_message.body and agent_reply.body
        nested inside each turn, not just to tool call args/results.
        Synthetic markers per CLAUDE.md PII rules; none of these are real."""
        consenter = await _consenting_user(async_db)
        async with async_db() as db:
            cs = ChatSession(
                session_id=f"sess-{uuid.uuid4().hex[:8]}",
                user_id=consenter.id,
                channel="imessage",
            )
            db.add(cs)
            await db.flush()
            db.add_all(
                [
                    Message(
                        session_id=cs.id,
                        seq=1,
                        direction="inbound",
                        body="Call me at +15555550123 anytime",
                    ),
                    Message(
                        session_id=cs.id,
                        seq=2,
                        direction="outbound",
                        body=(
                            "Email plumber@example.test for the quote. "
                            "Card 4111 1111 1111 1111 expires soon"
                        ),
                    ),
                ]
            )
            await db.commit()

        resp = await async_client.get(
            f"/api/admin/shared-data/users/{consenter.id}/conversation/turns"
        )
        assert resp.status_code == 200
        body_text = resp.text
        assert "+15555550123" not in body_text
        assert "plumber@example.test" not in body_text
        assert "4111 1111 1111 1111" not in body_text
        assert "[PHONE]" in body_text
        assert "[EMAIL]" in body_text
        assert "[CARD]" in body_text

    async def test_groups_messages_into_turns(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """Inbound + outbound pair becomes one turn with both messages and the tool list."""
        consenter = await _consenting_user(async_db)
        await _make_session_with_tools(async_db, consenter)

        resp = await async_client.get(
            f"/api/admin/shared-data/users/{consenter.id}/conversation/turns"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert len(data["turns"]) == 1
        turn = data["turns"][0]
        assert turn["turn_index"] == 0
        assert turn["user_message"]["seq"] == 1
        assert turn["user_message"]["direction"] == "inbound"
        assert turn["agent_reply"]["seq"] == 2
        assert turn["agent_reply"]["direction"] == "outbound"
        assert len(turn["tool_calls"]) == 2
        names = [t["name"] for t in turn["tool_calls"]]
        assert names == ["qb_query", "companycam_search_projects"]
        # Error flag survives the round trip.
        assert turn["tool_calls"][1]["is_error"] is True

    async def test_redacts_pii_from_tool_args_and_results(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """Recursive redaction: customer names buried in args / phone numbers in results."""
        consenter = await _consenting_user(async_db)
        await _make_session_with_tools(async_db, consenter)

        resp = await async_client.get(
            f"/api/admin/shared-data/users/{consenter.id}/conversation/turns"
        )
        assert resp.status_code == 200
        body = resp.text
        # Phone number planted inside the tool's result must be redacted.
        assert "+15555550123" not in body
        assert "[PHONE]" in body
        # The query string itself remains so admins can still see WHAT
        # the agent queried; only PII shapes inside it get redacted.
        assert "qb_query" in body

    async def test_agent_initiated_turn_has_no_user_message(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """A conversation that begins with an outbound (heartbeat-style)
        still surfaces. the leading turn just has user_message=None."""
        consenter = await _consenting_user(async_db)
        async with async_db() as db:
            cs = ChatSession(
                session_id=f"sess-{uuid.uuid4().hex[:8]}",
                user_id=consenter.id,
                channel="imessage",
            )
            db.add(cs)
            await db.flush()
            db.add(
                Message(
                    session_id=cs.id,
                    seq=1,
                    direction="outbound",
                    body="Heartbeat: checking in on your day",
                )
            )
            await db.commit()

        resp = await async_client.get(
            f"/api/admin/shared-data/users/{consenter.id}/conversation/turns"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["turns"][0]["user_message"] is None
        assert data["turns"][0]["agent_reply"]["seq"] == 1

    async def test_surfaces_thinking_text_redacted_on_agent_reply(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """Outbound messages with captured ``thinking_text`` (OSS migration
        033) must surface as ``agent_reply.thinking`` on the turn endpoint,
        PII-redacted with the same rules as ``body``. Inbound rows always
        have an empty thinking field. Admins use this to expand
        per-response reasoning in the activity pane (issue #456).
        """
        consenter = await _consenting_user(async_db)
        async with async_db() as db:
            cs = ChatSession(
                session_id=f"sess-{uuid.uuid4().hex[:8]}",
                user_id=consenter.id,
                channel="imessage",
            )
            db.add(cs)
            await db.flush()
            db.add_all(
                [
                    Message(
                        session_id=cs.id,
                        seq=1,
                        direction="inbound",
                        body="please email reminder@example.com",
                    ),
                    Message(
                        session_id=cs.id,
                        seq=2,
                        direction="outbound",
                        body="Sent.",
                        thinking_text=(
                            "The user wants me to email reminder@example.com. "
                            "Phone on file is +15555550123. Will call send_email."
                        ),
                    ),
                ]
            )
            await db.commit()

        resp = await async_client.get(
            f"/api/admin/shared-data/users/{consenter.id}/conversation/turns"
        )
        assert resp.status_code == 200
        data = resp.json()
        turn = data["turns"][0]
        # Inbound rows have no thinking; the schema default is empty string.
        assert turn["user_message"]["thinking"] == ""
        # Outbound thinking surfaces and is redacted at the leaves.
        assert turn["agent_reply"]["thinking"]
        body_text = resp.text
        assert "reminder@example.com" not in body_text
        assert "+15555550123" not in body_text
        assert "[EMAIL]" in body_text
        assert "[PHONE]" in body_text
        # The non-PII reasoning narrative survives so an admin can read
        # what the agent decided.
        assert "send_email" in turn["agent_reply"]["thinking"]

    async def test_writes_audit_row_with_correct_action(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
        db_session: Session,
    ) -> None:
        """The /turns route must use its own AdminAction so audit
        queries can distinguish 'admin viewed messages' from 'admin
        viewed turn-grouped tool calls'."""
        consenter = await _consenting_user(async_db)
        cs = await _make_session_with_tools(async_db, consenter)

        resp = await async_client.get(
            f"/api/admin/shared-data/users/{consenter.id}/conversation/turns"
        )
        assert resp.status_code == 200

        row = (
            db_session.query(AdminAuditLog)
            .filter(AdminAuditLog.action == "view_shared_data_conversation_turns")
            .order_by(AdminAuditLog.id.desc())
            .first()
        )
        assert row is not None
        assert row.target_user_id == consenter.id
        assert row.resource_type == "conversation"
        assert row.resource_id == cs.session_id

    async def test_limit_returns_most_recent_messages_not_oldest(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """When the conversation has more messages than ``limit``, the
        endpoint must clip the OLDEST rows, not the most recent ones.

        Trimmed messages (seq <= last_trim_seq) stay in the DB after
        compaction. A long-running user's session can carry hundreds of
        pre-trim rows. An ASC + limit query burns its budget on that
        history and silently drops the live tail the admin opened the
        page to see, while the conversation summary still reports
        message_count growing past the cap. Production case: a 542-
        message session with the frontend's default ``limit=500`` showed
        the admin nothing past seq ~500, hiding 19 minutes of recent
        agent activity (#TBD).
        """
        consenter = await _consenting_user(async_db)
        async with async_db() as db:
            cs = ChatSession(
                session_id=f"sess-{uuid.uuid4().hex[:8]}",
                user_id=consenter.id,
                channel="imessage",
            )
            db.add(cs)
            await db.flush()
            db.add_all(
                [
                    Message(
                        session_id=cs.id,
                        seq=s,
                        direction="inbound" if s % 2 == 1 else "outbound",
                        body=f"msg-{s}",
                    )
                    for s in range(1, 11)
                ]
            )
            await db.commit()

        resp = await async_client.get(
            f"/api/admin/shared-data/users/{consenter.id}/conversation/turns?limit=4"
        )
        assert resp.status_code == 200
        data = resp.json()

        seqs: list[int] = []
        for turn in data["turns"]:
            if turn["user_message"] is not None:
                seqs.append(turn["user_message"]["seq"])
            if turn["agent_reply"] is not None:
                seqs.append(turn["agent_reply"]["seq"])

        assert seqs, "expected at least one message in the response"
        assert max(seqs) == 10, f"latest seq must be present after limit; got {sorted(seqs)}"
        assert min(seqs) >= 7, f"oldest 6 messages should have been clipped; got {sorted(seqs)}"


# ---------------------------------------------------------------------------
# Profile / heartbeat / memory views (per-user, not per-conversation).
# ---------------------------------------------------------------------------


async def _consenting_user_with_profile(
    async_db: async_sessionmaker,
    *,
    soul: str = "",
    user_text: str = "",
    heartbeat: str = "",
    heartbeat_opt_in: bool = True,
    heartbeat_frequency: str = "30m",
    heartbeat_max_daily: int = 5,
) -> User:
    """Create a consenting user with the agent personality fields populated."""
    return _shared_user(
        consent=True,
        email="profile@example.com",
        soul_text=soul,
        user_text=user_text,
        heartbeat_text=heartbeat,
        heartbeat_opt_in=heartbeat_opt_in,
        heartbeat_frequency=heartbeat_frequency,
        heartbeat_max_daily=heartbeat_max_daily,
    )


class TestSharedDataProfile:
    async def test_403_for_non_consenting_user(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        silent = await _non_consenting_user(async_db)
        resp = await async_client.get(f"/api/admin/shared-data/users/{silent.id}/profile")
        assert resp.status_code == 403

    async def test_404_for_unknown_user(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        resp = await async_client.get(f"/api/admin/shared-data/users/{uuid.uuid4()}/profile")
        assert resp.status_code == 404

    async def test_returns_redacted_profile_text_and_heartbeat_config(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """Soul / user / heartbeat strings come back redacted; heartbeat
        config metadata comes back verbatim (not content)."""
        consenter = await _consenting_user_with_profile(
            async_db,
            soul="The agent should call them by their nickname.",
            user_text="They use a phone +15555550123 to text most often.",
            heartbeat="Email reminder@example.com every Friday morning.",
            heartbeat_opt_in=True,
            heartbeat_frequency="2h",
            heartbeat_max_daily=10,
        )

        resp = await async_client.get(f"/api/admin/shared-data/users/{consenter.id}/profile")
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == consenter.id
        assert data["consent_at"] is not None
        assert "nickname" in data["soul_text"]
        assert "+15555550123" not in data["user_text"]
        assert "[PHONE]" in data["user_text"]
        assert "reminder@example.com" not in data["heartbeat_text"]
        assert "[EMAIL]" in data["heartbeat_text"]
        assert data["heartbeat_opt_in"] is True
        assert data["heartbeat_frequency"] == "2h"
        assert data["heartbeat_max_daily"] == 10

    async def test_writes_audit_row(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
        db_session: Session,
    ) -> None:
        consenter = await _consenting_user_with_profile(async_db)
        resp = await async_client.get(f"/api/admin/shared-data/users/{consenter.id}/profile")
        assert resp.status_code == 200
        row = (
            db_session.query(AdminAuditLog)
            .filter(AdminAuditLog.action == "view_shared_data_profile")
            .order_by(AdminAuditLog.id.desc())
            .first()
        )
        assert row is not None
        assert row.target_user_id == consenter.id


async def _add_log(
    async_db: async_sessionmaker,
    user_id: str,
    *,
    action_type: str = "send",
    message: str = "",
    reasoning: str = "",
    tasks: str = "",
) -> None:
    from backend.app.models import HeartbeatLog

    async with async_db() as db:
        db.add(
            HeartbeatLog(
                user_id=user_id,
                action_type=action_type,
                channel="telegram",
                message_text=message,
                reasoning=reasoning,
                tasks=tasks,
            )
        )
        await db.commit()


class TestSharedDataHeartbeatLogs:
    async def test_403_for_non_consenting_user(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        silent = await _non_consenting_user(async_db)
        resp = await async_client.get(f"/api/admin/shared-data/users/{silent.id}/heartbeat-logs")
        assert resp.status_code == 403

    async def test_returns_redacted_content_columns(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """The encrypted-at-rest content columns must surface via this
        path (the slim /admin/users/{id}/heartbeat-logs dropped them)
        and must redact PII shapes before sending the response."""
        consenter = await _consenting_user(async_db)
        await _add_log(
            async_db,
            consenter.id,
            action_type="send",
            message="Reminder: pay invoice due Friday.",
            reasoning="User asked at +15555550123 to be nudged about payments.",
            tasks='[{"title": "Pay invoice", "due": "Friday"}]',
        )

        resp = await async_client.get(f"/api/admin/shared-data/users/{consenter.id}/heartbeat-logs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        item = data["items"][0]
        assert item["action_type"] == "send"
        assert item["channel"] == "telegram"
        assert "Reminder" in item["message_text"]
        assert "+15555550123" not in item["reasoning"]
        assert "[PHONE]" in item["reasoning"]
        assert "Pay invoice" in item["tasks"]

    async def test_writes_audit_row(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
        db_session: Session,
    ) -> None:
        consenter = await _consenting_user(async_db)
        resp = await async_client.get(f"/api/admin/shared-data/users/{consenter.id}/heartbeat-logs")
        assert resp.status_code == 200
        row = (
            db_session.query(AdminAuditLog)
            .filter(AdminAuditLog.action == "view_shared_data_heartbeat_logs")
            .order_by(AdminAuditLog.id.desc())
            .first()
        )
        assert row is not None
        assert row.target_user_id == consenter.id


class TestSharedDataMemory:
    async def test_403_for_non_consenting_user(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        silent = await _non_consenting_user(async_db)
        resp = await async_client.get(f"/api/admin/shared-data/users/{silent.id}/memory")
        assert resp.status_code == 403

    async def test_returns_empty_strings_when_user_has_no_memory_doc(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        consenter = await _consenting_user(async_db)
        resp = await async_client.get(f"/api/admin/shared-data/users/{consenter.id}/memory")
        assert resp.status_code == 200
        data = resp.json()
        assert data["memory_text"] == ""
        assert data["history_text"] == ""
        assert data["updated_at"] is None

    async def test_returns_redacted_memory_and_history(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """memory_text + history_text come back, PII-redacted at the leaves."""
        from backend.app.models import MemoryDocument

        consenter = await _consenting_user(async_db)
        async with async_db() as db:
            db.add(
                MemoryDocument(
                    user_id=consenter.id,
                    memory_text="Likes coffee. Phone is +15555550123.",
                    history_text=(
                        "Compacted 2026-04-15: discussed kitchen remodel. "
                        "Customer email plumber@example.test was mentioned."
                    ),
                )
            )
            await db.commit()

        resp = await async_client.get(f"/api/admin/shared-data/users/{consenter.id}/memory")
        assert resp.status_code == 200
        data = resp.json()
        assert "Likes coffee" in data["memory_text"]
        assert "+15555550123" not in data["memory_text"]
        assert "[PHONE]" in data["memory_text"]
        assert "kitchen remodel" in data["history_text"]
        assert "plumber@example.test" not in data["history_text"]
        assert "[EMAIL]" in data["history_text"]

    async def test_writes_audit_row(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
        db_session: Session,
    ) -> None:
        consenter = await _consenting_user(async_db)
        resp = await async_client.get(f"/api/admin/shared-data/users/{consenter.id}/memory")
        assert resp.status_code == 200
        row = (
            db_session.query(AdminAuditLog)
            .filter(AdminAuditLog.action == "view_shared_data_memory")
            .order_by(AdminAuditLog.id.desc())
            .first()
        )
        assert row is not None
        assert row.target_user_id == consenter.id


async def _add_compaction_event(
    async_db: async_sessionmaker,
    user_id: str,
    *,
    trimmed_count: int = 5,
    trimmed_chars: int = 1500,
    duration_ms: int = 800,
    input_tokens: int = 4000,
    output_tokens: int = 200,
    max_message_seq: int | None = 12,
    memory_updated: bool = True,
    user_profile_updated: bool = False,
    soul_updated: bool = False,
    summary_len: int = 120,
) -> int:
    from backend.app.models import CompactionEvent

    async with async_db() as db:
        event = CompactionEvent(
            user_id=user_id,
            trimmed_count=trimmed_count,
            trimmed_chars=trimmed_chars,
            duration_ms=duration_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            max_message_seq=max_message_seq,
            memory_updated=memory_updated,
            user_profile_updated=user_profile_updated,
            soul_updated=soul_updated,
            summary_len=summary_len,
        )
        db.add(event)
        await db.commit()
        await db.refresh(event)
        event_id = event.id
    return event_id


class TestSharedDataCompactionEvents:
    """Tests for the /compaction-events endpoint backed by OSS migration 023."""

    async def test_403_for_non_consenting_user(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        silent = await _non_consenting_user(async_db)
        resp = await async_client.get(f"/api/admin/shared-data/users/{silent.id}/compaction-events")
        assert resp.status_code == 403

    async def test_returns_empty_list_when_user_has_no_events(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        consenter = await _consenting_user(async_db)
        resp = await async_client.get(
            f"/api/admin/shared-data/users/{consenter.id}/compaction-events"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    async def test_returns_events_ordered_newest_first(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """Most recent compaction first. that's the question admins ask."""
        from sqlalchemy import update as sa_update

        from backend.app.models import CompactionEvent

        consenter = await _consenting_user(async_db)
        first_id = await _add_compaction_event(async_db, consenter.id, trimmed_count=3)
        # Bump triggered_at on the second so the order is deterministic
        # even when the test runs fast enough that both rows land in the
        # same millisecond.
        second_id = await _add_compaction_event(async_db, consenter.id, trimmed_count=10)
        async with async_db() as db:
            await db.execute(
                sa_update(CompactionEvent)
                .where(CompactionEvent.id == second_id)
                .values(triggered_at=_dt.datetime.now(_dt.UTC) + _dt.timedelta(seconds=1))
            )
            await db.commit()

        resp = await async_client.get(
            f"/api/admin/shared-data/users/{consenter.id}/compaction-events"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert [item["id"] for item in data["items"]] == [second_id, first_id]
        assert data["items"][0]["trimmed_count"] == 10
        assert data["items"][0]["memory_updated"] is True
        assert data["items"][0]["user_profile_updated"] is False

    async def test_writes_audit_row(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
        db_session: Session,
    ) -> None:
        consenter = await _consenting_user(async_db)
        resp = await async_client.get(
            f"/api/admin/shared-data/users/{consenter.id}/compaction-events"
        )
        assert resp.status_code == 200
        row = (
            db_session.query(AdminAuditLog)
            .filter(AdminAuditLog.action == "view_shared_data_compaction_events")
            .order_by(AdminAuditLog.id.desc())
            .first()
        )
        assert row is not None
        assert row.target_user_id == consenter.id

    async def test_returns_status_and_seq_range_and_empty_snapshots_by_default(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """A row written without snapshots reports status='completed'
        and empty snapshot envelopes (text=None, truncated=False).

        Mirrors the legacy / pre-feature row shape from migration 030's
        server_default plus the skip-if-unchanged optimization in
        compact_session: when the LLM didn't touch a file, the column
        stays NULL and the wire response shows an empty snapshot rather
        than crashing on missing required fields.
        """
        from backend.app.models import CompactionEvent

        consenter = await _consenting_user(async_db)
        async with async_db() as db:
            event = CompactionEvent(
                user_id=consenter.id,
                trimmed_count=2,
                min_message_seq=1,
                max_message_seq=10,
            )
            db.add(event)
            await db.commit()

        resp = await async_client.get(
            f"/api/admin/shared-data/users/{consenter.id}/compaction-events"
        )
        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert item["status"] == "completed"
        assert item["min_message_seq"] == 1
        assert item["max_message_seq"] == 10
        for col in (
            "memory_text_before",
            "memory_text_after",
            "history_text_before",
            "history_text_after",
            "user_text_before",
            "user_text_after",
            "soul_text_before",
            "soul_text_after",
        ):
            snap = item[col]
            assert snap["text"] is None, col
            assert snap["truncated"] is False, col

    async def test_pending_event_surfaces_status_pending(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """A row inserted by trigger_compaction_for_dropped before the
        async LLM call lands shows status='pending' to the admin,
        which lets the UI flag crashed-or-running compactions distinctly
        from completed ones.
        """
        from backend.app.models import CompactionEvent

        consenter = await _consenting_user(async_db)
        async with async_db() as db:
            event = CompactionEvent(
                user_id=consenter.id,
                status="pending",
                min_message_seq=5,
                max_message_seq=20,
                trimmed_count=15,
            )
            db.add(event)
            await db.commit()

        resp = await async_client.get(
            f"/api/admin/shared-data/users/{consenter.id}/compaction-events"
        )
        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert item["status"] == "pending"

    async def test_snapshot_plaintext_decrypts_to_response_body(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """A populated snapshot column round-trips through the ORM's
        envelope decryption to plaintext in the response.

        The point of Layer 4 is that an admin can see the diff between
        memory_text_before and memory_text_after for a given event;
        this asserts the wire format actually carries it.
        """
        from backend.app.models import CompactionEvent

        consenter = await _consenting_user(async_db)
        async with async_db() as db:
            event = CompactionEvent(
                user_id=consenter.id,
                memory_text_before="MEMORY before",
                memory_text_after="MEMORY after",
                history_text_after="History entry appended.",
                memory_updated=True,
            )
            db.add(event)
            await db.commit()

        resp = await async_client.get(
            f"/api/admin/shared-data/users/{consenter.id}/compaction-events"
        )
        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert item["memory_text_before"]["text"] == "MEMORY before"
        assert item["memory_text_before"]["truncated"] is False
        assert item["memory_text_after"]["text"] == "MEMORY after"
        assert item["history_text_after"]["text"] == "History entry appended."
        assert item["history_text_before"]["text"] is None

    async def test_truncation_record_is_surfaced_as_flagged_payload(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """When OSS truncates an oversized snapshot, the column carries
        the JSON envelope ``{"truncated": true, "size_bytes", "head",
        "tail", "sha256"}``. The endpoint must split that into structured
        fields so the admin UI can render "truncated, N KB" with the
        head and tail visible inline rather than dumping the JSON
        verbatim into a body cell.
        """
        import json as _json

        from backend.app.models import CompactionEvent

        consenter = await _consenting_user(async_db)
        record = _json.dumps(
            {
                "truncated": True,
                "size_bytes": 250_000,
                "head": "FIRST 2KB OF MEMORY",
                "tail": "LAST 2KB OF MEMORY",
                "sha256": "deadbeef" * 8,
            }
        )
        async with async_db() as db:
            event = CompactionEvent(
                user_id=consenter.id,
                memory_text_after=record,
                memory_updated=True,
            )
            db.add(event)
            await db.commit()

        resp = await async_client.get(
            f"/api/admin/shared-data/users/{consenter.id}/compaction-events"
        )
        assert resp.status_code == 200
        snap = resp.json()["items"][0]["memory_text_after"]
        assert snap["truncated"] is True
        assert snap["size_bytes"] == 250_000
        assert snap["head"] == "FIRST 2KB OF MEMORY"
        assert snap["tail"] == "LAST 2KB OF MEMORY"
        assert snap["sha256"] == "deadbeef" * 8
        # The plaintext text field stays None when truncated so the UI
        # never confuses the JSON envelope with real plaintext content.
        assert snap["text"] is None

    async def test_user_authored_json_is_not_misclassified_as_truncation(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """A user who pastes ``{"truncated": true}`` into MEMORY.md
        must NOT trigger the "truncated" UI banner. The decoder
        requires both ``truncated=true`` AND a numeric ``size_bytes``
        before treating a column as the truncation envelope.
        """
        from backend.app.models import CompactionEvent

        consenter = await _consenting_user(async_db)
        async with async_db() as db:
            event = CompactionEvent(
                user_id=consenter.id,
                memory_text_after='{"truncated": true, "note": "user pasted this"}',
            )
            db.add(event)
            await db.commit()

        resp = await async_client.get(
            f"/api/admin/shared-data/users/{consenter.id}/compaction-events"
        )
        assert resp.status_code == 200
        snap = resp.json()["items"][0]["memory_text_after"]
        assert snap["truncated"] is False
        assert snap["text"] == '{"truncated": true, "note": "user pasted this"}'

    async def test_snapshot_text_is_pii_redacted(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """Snapshot plaintext must run through ``redact_pii`` before
        leaving the endpoint, matching the existing /memory and
        /profile content surfaces. Without this, phone numbers and
        emails the agent extracted into MEMORY.md would surface
        verbatim through the per-event snapshots even though the
        sibling endpoints redact the same content.
        """
        from backend.app.models import CompactionEvent

        consenter = await _consenting_user(async_db)
        async with async_db() as db:
            event = CompactionEvent(
                user_id=consenter.id,
                memory_text_before="Call John at +15555550123 or john@example.com",
                memory_text_after="Updated phone: +15555550456",
            )
            db.add(event)
            await db.commit()

        resp = await async_client.get(
            f"/api/admin/shared-data/users/{consenter.id}/compaction-events"
        )
        assert resp.status_code == 200
        snaps = resp.json()["items"][0]
        before = snaps["memory_text_before"]["text"]
        after = snaps["memory_text_after"]["text"]
        assert "+15555550123" not in before
        assert "+15555550456" not in after
        assert "john@example.com" not in before
        assert "[PHONE]" in before
        assert "[EMAIL]" in before
        assert "[PHONE]" in after

    async def test_truncation_record_head_and_tail_are_redacted(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """The ``head`` and ``tail`` slices of a truncation record carry
        the same kind of memory-file content that the plaintext path
        carries; they must be redacted on the way out so an oversized
        snapshot does not become a PII bypass for the smaller
        plaintext path.
        """
        import json as _json

        from backend.app.models import CompactionEvent

        consenter = await _consenting_user(async_db)
        record = _json.dumps(
            {
                "truncated": True,
                "size_bytes": 250_000,
                "head": "First part: contact +15555550123",
                "tail": "Last part: write to jane@example.com",
                "sha256": "deadbeef" * 8,
            }
        )
        async with async_db() as db:
            event = CompactionEvent(user_id=consenter.id, memory_text_after=record)
            db.add(event)
            await db.commit()

        resp = await async_client.get(
            f"/api/admin/shared-data/users/{consenter.id}/compaction-events"
        )
        assert resp.status_code == 200
        snap = resp.json()["items"][0]["memory_text_after"]
        assert snap["truncated"] is True
        assert "+15555550123" not in snap["head"]
        assert "[PHONE]" in snap["head"]
        assert "jane@example.com" not in snap["tail"]
        assert "[EMAIL]" in snap["tail"]
        # size_bytes / sha256 describe the original plaintext, not user
        # content, so they ride through unchanged.
        assert snap["size_bytes"] == 250_000
        assert snap["sha256"] == "deadbeef" * 8

    async def test_llm_call_capture_columns_round_trip_redacted(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """OSS migration 031 added prompt_text / raw_response_text /
        parsed_response_json. The endpoint must surface them through
        the same snapshot envelope as the memory-file diffs and the
        same redaction pass; without this, an admin reading a
        compaction event could see verbatim phone numbers or emails
        in the trimmed conversation that was sent to the LLM.
        """
        from backend.app.models import CompactionEvent

        consenter = await _consenting_user(async_db)
        async with async_db() as db:
            event = CompactionEvent(
                user_id=consenter.id,
                prompt_text="User: please email john@example.com\nAssistant: ok",
                raw_response_text='{"memory_update": "noted email john@example.com"}',
                parsed_response_json=(
                    '{"memory_update": "noted email john@example.com",'
                    ' "summary": "", "user_profile_update": "",'
                    ' "soul_update": ""}'
                ),
            )
            db.add(event)
            await db.commit()

        resp = await async_client.get(
            f"/api/admin/shared-data/users/{consenter.id}/compaction-events"
        )
        assert resp.status_code == 200
        item = resp.json()["items"][0]
        # Each of the three new fields decodes via the same snapshot
        # envelope as the memory-file diffs.
        for key in ("prompt", "raw_response", "parsed_response"):
            snap = item[key]
            assert snap["truncated"] is False
            assert snap["text"] is not None
            # Same redaction pass as memory snapshots.
            assert "john@example.com" not in snap["text"], key
            assert "[EMAIL]" in snap["text"], key

    async def test_llm_call_capture_empty_when_pending(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """Pending events have not yet captured the LLM call. All three
        new fields must surface as empty (text=None, truncated=False)
        so the UI can render a 'still running' placeholder rather than
        crashing on missing data.
        """
        from backend.app.models import CompactionEvent

        consenter = await _consenting_user(async_db)
        async with async_db() as db:
            event = CompactionEvent(user_id=consenter.id, status="pending")
            db.add(event)
            await db.commit()

        resp = await async_client.get(
            f"/api/admin/shared-data/users/{consenter.id}/compaction-events"
        )
        assert resp.status_code == 200
        item = resp.json()["items"][0]
        for key in ("prompt", "raw_response", "parsed_response"):
            snap = item[key]
            assert snap["text"] is None, key
            assert snap["truncated"] is False, key


def _make_record(
    record_id: int,
    *,
    user_id: str,
    event_type: str = "requested",
    tool_name: str = "write_file",
    description: str = "write a file",
    channel: str = "telegram",
    chat_id: str = "chat_1",
    decision: str | None = None,
    created_at: _dt.datetime | None = None,
) -> ApprovalEventRecord:
    """Build an ApprovalEventRecord projection without touching the DB."""
    return ApprovalEventRecord(
        id=record_id,
        user_id=user_id,
        event_type=event_type,
        tool_name=tool_name,
        description=description,
        channel=channel,
        chat_id=chat_id,
        decision=decision,
        created_at=created_at or _dt.datetime.now(_dt.UTC),
    )


class TestSharedDataApprovalEvents:
    """Tests for the /approval-events endpoint backed by OSS migration 028.

    The route reads through the OSS ``ApprovalEventStore.list_for_user``,
    which is ``async def`` and opens its own ``db_session_async()``.
    These tests stub the store with a planted record list so the route's
    redaction, response shape, and audit row writes are what is under
    test here; the underlying SQL for ``list_for_user`` is covered by
    OSS's own ``test_approval`` suite.
    """

    async def test_403_for_non_consenting_user(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        silent = await _non_consenting_user(async_db)
        resp = await async_client.get(f"/api/admin/shared-data/users/{silent.id}/approval-events")
        assert resp.status_code == 403

    async def test_empty_when_no_events(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        consenter = await _consenting_user(async_db)
        with patch(
            "backend.app.routers.admin_shared_data.get_approval_event_store"
        ) as mock_store_factory:
            mock_store_factory.return_value.list_for_user = AsyncMock(return_value=[])
            resp = await async_client.get(
                f"/api/admin/shared-data/users/{consenter.id}/approval-events"
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    async def test_returns_lifecycle_pair_in_chronological_order(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """The activity feed reads request/decided pairs in order, so the
        endpoint must return ``requested`` before its matching ``decided``."""
        consenter = await _consenting_user(async_db)
        now = _dt.datetime.now(_dt.UTC)
        records = [
            _make_record(1, user_id=consenter.id, event_type="requested", created_at=now),
            _make_record(
                2,
                user_id=consenter.id,
                event_type="decided",
                decision="approved",
                created_at=now + _dt.timedelta(seconds=1),
            ),
        ]
        with patch(
            "backend.app.routers.admin_shared_data.get_approval_event_store"
        ) as mock_store_factory:
            mock_store_factory.return_value.list_for_user = AsyncMock(return_value=records)
            resp = await async_client.get(
                f"/api/admin/shared-data/users/{consenter.id}/approval-events"
            )
        assert resp.status_code == 200
        data = resp.json()
        assert [item["id"] for item in data["items"]] == [1, 2]
        assert data["items"][0]["event_type"] == "requested"
        assert data["items"][0]["decision"] is None
        assert data["items"][1]["event_type"] == "decided"
        assert data["items"][1]["decision"] == "approved"

    async def test_redacts_pii_in_description(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """``description`` echoes user-pasted content (filenames, URLs,
        quoted snippets) and must be PII-redacted before serialization
        the same way conversation bodies are."""
        consenter = await _consenting_user(async_db)
        records = [
            _make_record(
                1,
                user_id=consenter.id,
                description="reach out to nathan@mozilla.ai about the bug",
            )
        ]
        with patch(
            "backend.app.routers.admin_shared_data.get_approval_event_store"
        ) as mock_store_factory:
            mock_store_factory.return_value.list_for_user = AsyncMock(return_value=records)
            resp = await async_client.get(
                f"/api/admin/shared-data/users/{consenter.id}/approval-events"
            )
        assert resp.status_code == 200
        body = resp.json()["items"][0]["description"]
        assert "nathan@mozilla.ai" not in body

    async def test_writes_audit_row(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
        db_session: Session,
    ) -> None:
        consenter = await _consenting_user(async_db)
        with patch(
            "backend.app.routers.admin_shared_data.get_approval_event_store"
        ) as mock_store_factory:
            mock_store_factory.return_value.list_for_user = AsyncMock(return_value=[])
            resp = await async_client.get(
                f"/api/admin/shared-data/users/{consenter.id}/approval-events"
            )
        assert resp.status_code == 200
        row = (
            db_session.query(AdminAuditLog)
            .filter(AdminAuditLog.action == "view_shared_data_approval_events")
            .order_by(AdminAuditLog.id.desc())
            .first()
        )
        assert row is not None
        assert row.target_user_id == consenter.id


class TestSharedDataExport:
    """Composite per-user export endpoint.

    The export bundles every consent-gated surface for one user into a
    single response so a CLI caller can answer "what's wrong with this
    user's experience?" without walking the per-surface endpoints.
    """

    async def test_403_for_non_consenting_user(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        silent = await _non_consenting_user(async_db)
        resp = await async_client.get(f"/api/admin/shared-data/users/{silent.id}/export")
        assert resp.status_code == 403

    async def test_404_for_unknown_user(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        resp = await async_client.get(f"/api/admin/shared-data/users/{uuid.uuid4()}/export")
        assert resp.status_code == 404

    async def test_returns_summary_and_section_skeleton(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """Empty user: every section is present with zero counts."""
        consenter = await _consenting_user(async_db)

        resp = await async_client.get(f"/api/admin/shared-data/users/{consenter.id}/export?days=7")
        assert resp.status_code == 200
        body = resp.json()
        # Top-level shape is the contract. every consumer relies on these fields.
        assert body["user_id"] == consenter.id
        for key in (
            "user",
            "window",
            "summary",
            "conversations",
            "heartbeat_logs",
            "compaction_events",
            "profile",
            "memory",
        ):
            assert key in body, f"missing top-level key {key}"
        assert body["window"]["days"] == 7
        assert body["summary"]["session_count"] == 0
        assert body["summary"]["heartbeat_directives_count"] == 0
        # ``include_turns`` defaults to False; turns is null when omitted.
        assert body["turns"] is None

    async def test_summary_aggregates_window_activity(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        from backend.app.models import HeartbeatLog

        consenter = await _consenting_user(async_db, email="active@example.com")
        async with async_db() as db:
            cs = ChatSession(
                user_id=consenter.id,
                session_id=f"sess-{uuid.uuid4().hex[:8]}",
                channel="imessage",
                last_message_at=_dt.datetime.now(_dt.UTC),
            )
            db.add(cs)
            await db.flush()
            db.add_all(
                [
                    Message(
                        session_id=cs.id,
                        seq=1,
                        direction="inbound",
                        body="hello",
                        timestamp=_dt.datetime.now(_dt.UTC),
                    ),
                    Message(
                        session_id=cs.id,
                        seq=2,
                        direction="outbound",
                        body="hi",
                        timestamp=_dt.datetime.now(_dt.UTC),
                        tool_interactions_json=(
                            '[{"tool_call_id":"t1","name":"calculate",'
                            '"args":{},"result":"4","is_error":false,'
                            '"receipt":null}]'
                        ),
                    ),
                    HeartbeatLog(
                        user_id=consenter.id,
                        action_type="skip",
                        channel="imessage",
                        created_at=_dt.datetime.now(_dt.UTC),
                    ),
                    HeartbeatLog(
                        user_id=consenter.id,
                        action_type="send",
                        channel="imessage",
                        created_at=_dt.datetime.now(_dt.UTC),
                    ),
                ]
            )
            await db.commit()

        resp = await async_client.get(f"/api/admin/shared-data/users/{consenter.id}/export?days=7")
        assert resp.status_code == 200
        s = resp.json()["summary"]
        assert s["session_count"] == 1
        assert s["inbound_count"] == 1
        assert s["outbound_count"] == 1
        assert s["heartbeats_total"] == 2
        assert s["heartbeats_by_action"]["skip"] == 1
        assert s["heartbeats_by_action"]["send"] == 1
        # Tool call surfaced from the outbound message's tool_interactions_json.
        assert s["tool_calls_total"] == 1
        assert s["tool_calls_error_count"] == 0
        assert any(t["name"] == "calculate" and t["call_count"] == 1 for t in s["tool_calls_top"])

    async def test_window_excludes_old_activity(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """Sessions older than the window are not counted toward summary.

        Pinning this so a future change to the window math doesn't
        silently start including ancient sessions.
        """
        consenter = await _consenting_user(async_db)
        async with async_db() as db:
            old = ChatSession(
                user_id=consenter.id,
                session_id=f"sess-{uuid.uuid4().hex[:8]}",
                channel="imessage",
                last_message_at=_dt.datetime.now(_dt.UTC) - _dt.timedelta(days=30),
            )
            db.add(old)
            await db.commit()

        resp = await async_client.get(f"/api/admin/shared-data/users/{consenter.id}/export?days=7")
        assert resp.status_code == 200
        assert resp.json()["summary"]["session_count"] == 0

    async def test_include_turns_attaches_transcripts(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        consenter = await _consenting_user(async_db)
        async with async_db() as db:
            cs = ChatSession(
                user_id=consenter.id,
                session_id=f"sess-{uuid.uuid4().hex[:8]}",
                channel="imessage",
                last_message_at=_dt.datetime.now(_dt.UTC),
            )
            db.add(cs)
            await db.flush()
            db.add_all(
                [
                    Message(
                        session_id=cs.id,
                        seq=1,
                        direction="inbound",
                        body="hi",
                        timestamp=_dt.datetime.now(_dt.UTC),
                    ),
                    Message(
                        session_id=cs.id,
                        seq=2,
                        direction="outbound",
                        body="hello",
                        timestamp=_dt.datetime.now(_dt.UTC),
                    ),
                ]
            )
            await db.commit()
            session_id_str = cs.session_id

        # Default (no flag): turns is null.
        resp_off = await async_client.get(f"/api/admin/shared-data/users/{consenter.id}/export")
        assert resp_off.json()["turns"] is None

        # Opt-in: turns array with one entry per session.
        resp_on = await async_client.get(
            f"/api/admin/shared-data/users/{consenter.id}/export?include_turns=true"
        )
        assert resp_on.status_code == 200
        turns = resp_on.json()["turns"]
        assert isinstance(turns, list) and len(turns) == 1
        assert turns[0]["session_id"] == session_id_str

    async def test_include_turns_returns_full_session_history(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """When a session is in the window, ``include_turns=true`` returns
        every message in that session, not just the messages whose
        ``timestamp`` falls inside the window. Pinning the intent so a
        well-meaning contributor does not "fix" the perceived bug.

        The reasoning: ``window`` scopes which sessions show up
        (``ChatSession.last_message_at >= window_start``), and counts
        like ``message_count`` and the tool-call rollup are also
        windowed. But once an admin opens an in-window session for
        debugging, they want the full conversation thread, not a
        truncated view that cuts off mid-back-and-forth at the window
        boundary. A user who messages once today on a months-long
        thread should still surface the thread's full history when
        pulled via ``include_turns=true``.
        """
        consenter = await _consenting_user(async_db, email="long-thread@example.com")
        async with async_db() as db:
            cs = ChatSession(
                user_id=consenter.id,
                session_id=f"sess-{uuid.uuid4().hex[:8]}",
                channel="imessage",
                last_message_at=_dt.datetime.now(_dt.UTC),
            )
            db.add(cs)
            await db.flush()
            now = _dt.datetime.now(_dt.UTC)
            # Two ancient messages (well outside the days=7 window) and one
            # recent message. The recent one keeps last_message_at inside
            # the window so the session is included; the ancient ones must
            # still surface in turns.
            db.add_all(
                [
                    Message(
                        session_id=cs.id,
                        seq=1,
                        direction="inbound",
                        body="ancient inbound",
                        timestamp=now - _dt.timedelta(days=60),
                    ),
                    Message(
                        session_id=cs.id,
                        seq=2,
                        direction="outbound",
                        body="ancient reply",
                        timestamp=now - _dt.timedelta(days=60),
                    ),
                    Message(
                        session_id=cs.id,
                        seq=3,
                        direction="inbound",
                        body="fresh inbound",
                        timestamp=now,
                    ),
                ]
            )
            await db.commit()

        resp = await async_client.get(
            f"/api/admin/shared-data/users/{consenter.id}/export?days=7&include_turns=true"
        )
        assert resp.status_code == 200
        turns = resp.json()["turns"]
        assert isinstance(turns, list) and len(turns) == 1
        # All three messages surface as two turns (ancient inbound +
        # ancient outbound = turn 0; fresh inbound = turn 1 with no
        # reply yet). The ancient pair would be cut off if the bulk
        # fetch grew a Message.timestamp >= window_start filter.
        session_turns = turns[0]["turns"]
        assert len(session_turns) == 2

    async def test_writes_audit_row_with_export_action(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
        db_session: Session,
    ) -> None:
        consenter = await _consenting_user(async_db)
        before = db_session.query(AdminAuditLog).count()
        resp = await async_client.get(f"/api/admin/shared-data/users/{consenter.id}/export?days=3")
        assert resp.status_code == 200
        after = db_session.query(AdminAuditLog).count()
        assert after == before + 1
        latest = db_session.query(AdminAuditLog).order_by(AdminAuditLog.id.desc()).first()
        assert latest is not None
        assert latest.action == "view_shared_data_export"
        assert latest.target_user_id == consenter.id
        # Detail captures the args so a forensic query can answer
        # "did anyone pull turns for this user?".
        assert latest.detail is not None
        assert latest.detail.get("days") == 3
        assert latest.detail.get("include_turns") is False

    async def test_reports_total_is_window_scoped(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """``reports_total`` should respect the window like every other
        time-bucketed count in the rollup. A user with one ancient
        report and one fresh report under days=7 should see 1, not 2.
        """
        from backend.app.models import ReportedConversation

        consenter = await _consenting_user(async_db, email="reporter-windowed@example.com")
        async with async_db() as db:
            cs = ChatSession(
                user_id=consenter.id,
                session_id=f"sess-{uuid.uuid4().hex[:8]}",
                channel="imessage",
                last_message_at=_dt.datetime.now(_dt.UTC),
            )
            db.add(cs)
            await db.flush()
            now = _dt.datetime.now(_dt.UTC)
            db.add_all(
                [
                    ReportedConversation(
                        user_id=consenter.id,
                        session_id=cs.id,
                        reason="recent",
                        created_at=now - _dt.timedelta(days=1),
                    ),
                    ReportedConversation(
                        user_id=consenter.id,
                        session_id=cs.id,
                        reason="ancient",
                        created_at=now - _dt.timedelta(days=60),
                    ),
                ]
            )
            await db.commit()

        resp = await async_client.get(f"/api/admin/shared-data/users/{consenter.id}/export?days=7")
        assert resp.status_code == 200
        assert resp.json()["summary"]["reports_total"] == 1

    async def test_tool_calls_total_skips_inbound_messages(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """A stray ``tool_interactions_json`` payload on an inbound row
        must not bump the tool-call rollup. Outbound is the only
        direction that legitimately carries tool calls; the export
        filter pins that intent so a future change that ever populated
        the column on inbound rows would not double-count."""
        consenter = await _consenting_user(async_db, email="inbound-toolcheck@example.com")
        async with async_db() as db:
            cs = ChatSession(
                user_id=consenter.id,
                session_id=f"sess-{uuid.uuid4().hex[:8]}",
                channel="imessage",
                last_message_at=_dt.datetime.now(_dt.UTC),
            )
            db.add(cs)
            await db.flush()
            bogus_inbound_tool_blob = (
                '[{"tool_call_id":"x","name":"should_not_count","args":{},'
                '"result":"","is_error":false,"receipt":null}]'
            )
            legit_outbound_tool_blob = (
                '[{"tool_call_id":"y","name":"calculate","args":{},'
                '"result":"4","is_error":false,"receipt":null}]'
            )
            db.add_all(
                [
                    Message(
                        session_id=cs.id,
                        seq=1,
                        direction="inbound",
                        body="hi",
                        timestamp=_dt.datetime.now(_dt.UTC),
                        tool_interactions_json=bogus_inbound_tool_blob,
                    ),
                    Message(
                        session_id=cs.id,
                        seq=2,
                        direction="outbound",
                        body="ok",
                        timestamp=_dt.datetime.now(_dt.UTC),
                        tool_interactions_json=legit_outbound_tool_blob,
                    ),
                ]
            )
            await db.commit()

        resp = await async_client.get(f"/api/admin/shared-data/users/{consenter.id}/export?days=7")
        assert resp.status_code == 200
        s = resp.json()["summary"]
        assert s["tool_calls_total"] == 1
        names = [t["name"] for t in s["tool_calls_top"]]
        assert "calculate" in names
        assert "should_not_count" not in names
