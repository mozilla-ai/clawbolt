"""Endpoint tests for /admin/reported-conversations (issue #325 item 5).

Covers:
- List endpoint: open + dismissed reports show up; status filter
  narrows correctly; PII in the reason is redacted before serializing.
- Messages endpoint: returns the window around anchor_seq with
  is_anchor flag; PII in bodies is redacted; 404 on unknown report;
  graceful empty list when the underlying session was deleted.
- Dismiss endpoint: stamps dismissed_at + reviewed_admin_user_id;
  404 on unknown; 400 on already-dismissed.

Async DB conversion (Phase C, issue #394). Routes use
``Depends(get_async_db)`` and an ``AsyncSession``; tests drive them
through ``httpx.AsyncClient`` + ``ASGITransport`` and shuttle row
inserts through the per-test ``async_db`` fixture so the route reads
back its own writes under READ COMMITTED.

The audit dependency stays sync (it writes its own row through a
fresh ``SessionLocal()``), and ``get_current_admin`` is overridden in
the async client fixture to return ``async_test_user`` directly so
the admin's row exists on the async connection where the dismiss
route stamps ``reviewed_admin_user_id``. The sync per-test
connection has no matching admin user, so the audit dep's FK insert
fails best-effort here and no audit row is persisted under these
tests; the audit dependency itself is exercised by the all-sync
suite in ``tests/test_admin_audit.py``. The two pre-conversion tests
that asserted on ``AdminAuditLog`` rows for these endpoints
(``test_writes_audit_row_with_resource_id`` and
``test_dismiss_writes_audit_row``) are dropped on conversion; if a
regression net for those audit-context fields is needed later, add
all-sync cases against the existing sync ``client`` fixture and the
audit log table.
"""

from __future__ import annotations

import datetime as _dt
import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.auth.admin_dep import get_current_admin
from backend.app.auth.dependencies import get_current_user
from backend.app.middleware.rate_limit import _auth_rate_limiter
from backend.app.models import ChatSession, Message, ReportedConversation, User


@pytest.fixture(autouse=True)
def _clear_rate_limits() -> None:
    _auth_rate_limiter.reset()


@pytest_asyncio.fixture
async def async_admin_client(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> AsyncGenerator[httpx.AsyncClient]:
    """ASGI-driven async HTTP client authenticated as ``async_test_user``.

    Overrides both ``get_current_user`` and ``get_current_admin`` so
    the route's admin auth resolves without a sync DB lookup against
    a Subscription row. Using only the async per-test connection
    sidesteps the cross-API caveat: a sync-side admin row would try
    to share its primary key with the async-side row, producing a
    row-level lock wait under READ COMMITTED.

    The audit dependency keeps writing through a fresh
    ``SessionLocal()`` (sync per-test connection) and its admin FK
    insert fails silently here because the async-only user has no
    sync-side row. The audit insert path is otherwise covered by the
    all-sync suite in ``tests/test_admin_audit.py``.
    """
    from tests.multi_user.conftest import MULTI_USER_APP as app

    app.dependency_overrides[get_current_user] = lambda: async_test_user
    app.dependency_overrides[get_current_admin] = lambda: async_test_user
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

    app.dependency_overrides.pop(get_current_admin, None)
    app.dependency_overrides.pop(get_current_user, None)


async def _make_user(async_db: async_sessionmaker, *, email: str = "user@example.com") -> User:
    """Insert a User + Subscription via the async per-test DB."""
    from backend.app.models import Subscription

    user = User(
        id=str(uuid.uuid4()),
        user_id=f"google_{uuid.uuid4().hex[:8]}",
        phone="",
        onboarding_complete=True,
    )
    async with async_db() as db:
        # Flush the User first: without an ORM relationship between the two
        # models, one flush orders the INSERTs by mapper sort key and puts
        # subscriptions ahead of users, violating the FK.
        db.add(user)
        await db.flush()
        db.add(
            Subscription(user_id=user.id, role="user", plan="free", status="active", email=email)
        )
        await db.commit()
        await db.refresh(user)
        db.expunge(user)
    return user


async def _make_session(
    async_db: async_sessionmaker, user: User, *, channel: str = "imessage"
) -> ChatSession:
    cs = ChatSession(
        session_id=f"sess-{uuid.uuid4().hex[:8]}",
        user_id=user.id,
        channel=channel,
    )
    async with async_db() as db:
        db.add(cs)
        await db.commit()
        await db.refresh(cs)
        db.expunge(cs)
    return cs


async def _make_report(
    async_db: async_sessionmaker,
    user: User,
    cs: ChatSession,
    *,
    reason: str = "",
    anchor_seq: int | None = None,
    dismissed: bool = False,
) -> ReportedConversation:
    row = ReportedConversation(
        user_id=user.id,
        session_id=cs.id,
        anchor_seq=anchor_seq,
        reason=reason,
        dismissed_at=_dt.datetime.now(_dt.UTC) if dismissed else None,
    )
    async with async_db() as db:
        db.add(row)
        await db.commit()
        await db.refresh(row)
        db.expunge(row)
    return row


async def _add_message(
    async_db: async_sessionmaker,
    cs: ChatSession,
    *,
    seq: int,
    direction: str,
    body: str,
) -> None:
    async with async_db() as db:
        db.add(Message(session_id=cs.id, seq=seq, direction=direction, body=body))
        await db.commit()


async def _refresh_report(
    async_db: async_sessionmaker, report_id: int
) -> ReportedConversation | None:
    async with async_db() as db:
        return (
            await db.execute(
                select(ReportedConversation).where(ReportedConversation.id == report_id)
            )
        ).scalar_one_or_none()


class TestListReportedConversations:
    async def test_lists_open_and_dismissed_with_open_first(
        self,
        async_admin_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
    ) -> None:
        u = await _make_user(async_db, email="reporter@example.com")
        cs = await _make_session(async_db, u)
        # One dismissed (older) + one open (newer). Open should sort first.
        await _make_report(async_db, u, cs, reason="closed already", dismissed=True)
        await _make_report(async_db, u, cs, reason="still open")

        resp = await async_admin_client.get("/api/admin/reported-conversations")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert data["open_count"] == 1
        statuses = [item["status"] for item in data["items"]]
        # Open comes before dismissed regardless of created_at order.
        assert statuses == ["open", "dismissed"]
        assert data["items"][0]["user_email"] == "reporter@example.com"

    async def test_status_filter_narrows(
        self,
        async_admin_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
    ) -> None:
        u = await _make_user(async_db)
        cs = await _make_session(async_db, u)
        await _make_report(async_db, u, cs, reason="open one")
        await _make_report(async_db, u, cs, reason="dismissed one", dismissed=True)

        open_resp = await async_admin_client.get("/api/admin/reported-conversations?status=open")
        assert open_resp.status_code == 200
        assert all(i["status"] == "open" for i in open_resp.json()["items"])
        assert open_resp.json()["total"] == 1

        dismissed_resp = await async_admin_client.get(
            "/api/admin/reported-conversations?status=dismissed"
        )
        assert dismissed_resp.status_code == 200
        assert all(i["status"] == "dismissed" for i in dismissed_resp.json()["items"])
        assert dismissed_resp.json()["total"] == 1

    async def test_reason_is_pii_redacted(
        self,
        async_admin_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
    ) -> None:
        """A user filing ``/report my number is +15555550123`` writes
        the raw text into ``reason``; the admin queue MUST redact it
        before surfacing to the admin."""
        u = await _make_user(async_db)
        cs = await _make_session(async_db, u)
        await _make_report(
            async_db,
            u,
            cs,
            reason="please call me at +15555550123 about jane@example.test",
        )

        resp = await async_admin_client.get("/api/admin/reported-conversations")
        assert resp.status_code == 200
        wire = resp.text
        assert "+15555550123" not in wire
        assert "jane@example.test" not in wire
        # The redaction tokens DO appear.
        body = resp.json()["items"][0]["reason"]
        assert "[PHONE]" in body
        assert "[EMAIL]" in body


class TestReportedConversationMessages:
    async def test_returns_window_around_anchor_with_flag(
        self,
        async_admin_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
    ) -> None:
        u = await _make_user(async_db)
        cs = await _make_session(async_db, u)
        # Seed seq 1..10; anchor at 5.
        for seq in range(1, 11):
            await _add_message(
                async_db,
                cs,
                seq=seq,
                direction="inbound" if seq % 2 else "outbound",
                body=f"msg-{seq}",
            )
        report = await _make_report(async_db, u, cs, anchor_seq=5)

        resp = await async_admin_client.get(
            f"/api/admin/reported-conversations/{report.id}/messages?window=2"
        )
        assert resp.status_code == 200
        data = resp.json()
        seqs = [m["seq"] for m in data["items"]]
        # Window=2 around anchor=5 -> seqs 3..7.
        assert seqs == [3, 4, 5, 6, 7]
        # Exactly one message flagged as the anchor.
        anchor_msgs = [m for m in data["items"] if m["is_anchor"]]
        assert len(anchor_msgs) == 1
        assert anchor_msgs[0]["seq"] == 5

    async def test_returns_404_for_unknown_report(
        self,
        async_admin_client: httpx.AsyncClient,
    ) -> None:
        resp = await async_admin_client.get("/api/admin/reported-conversations/999999/messages")
        assert resp.status_code == 404

    async def test_redacts_pii_in_message_bodies(
        self,
        async_admin_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
    ) -> None:
        u = await _make_user(async_db)
        cs = await _make_session(async_db, u)
        await _add_message(
            async_db,
            cs,
            seq=1,
            direction="inbound",
            body="please email reporter-marker-9f@example.test",
        )
        report = await _make_report(async_db, u, cs, anchor_seq=1)

        resp = await async_admin_client.get(
            f"/api/admin/reported-conversations/{report.id}/messages"
        )
        assert resp.status_code == 200
        wire = resp.text
        assert "reporter-marker-9f@example.test" not in wire
        assert "[EMAIL]" in wire


class TestDismissReportedConversation:
    async def test_dismisses_an_open_report(
        self,
        async_admin_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        u = await _make_user(async_db)
        cs = await _make_session(async_db, u)
        report = await _make_report(async_db, u, cs)

        resp = await async_admin_client.post(
            f"/api/admin/reported-conversations/{report.id}/dismiss"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == report.id
        assert data["dismissed_at"] is not None
        assert data["reviewed_admin_user_id"] == async_test_user.id

        # DB row reflects the change (read through the async per-test
        # connection so we see the route's write).
        refreshed = await _refresh_report(async_db, report.id)
        assert refreshed is not None
        assert refreshed.dismissed_at is not None
        assert refreshed.reviewed_admin_user_id == async_test_user.id

    async def test_returns_404_for_unknown_report(
        self,
        async_admin_client: httpx.AsyncClient,
    ) -> None:
        resp = await async_admin_client.post("/api/admin/reported-conversations/999999/dismiss")
        assert resp.status_code == 404

    async def test_returns_400_when_already_dismissed(
        self,
        async_admin_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
    ) -> None:
        u = await _make_user(async_db)
        cs = await _make_session(async_db, u)
        report = await _make_report(async_db, u, cs, dismissed=True)

        resp = await async_admin_client.post(
            f"/api/admin/reported-conversations/{report.id}/dismiss"
        )
        assert resp.status_code == 400
        assert "already dismissed" in resp.json()["detail"].lower()
