"""Endpoint tests for admin router (issue #70).

Phase C async migration (issue #392). The router uses ``Depends(get_async_db)``,
so tests drive it through an ``httpx.AsyncClient`` + ``ASGITransport`` and use
the ``async_db`` fixture to share a per-test connection with the route. Setup
and verification go through ``async_db()`` rather than the sync ``SessionLocal()``
because the async fixture's connection lives on a different backend connection
from the sync ``_isolate_stores`` fixture (see the design comment in
``conftest.py``); a row written through the sync connection is invisible to the
async route under READ COMMITTED.

Covers:
- Admin access control (role check, 403 for non-admins)
- GET /api/admin/users (list, search, pagination)
- POST /api/admin/users/{id}/activate
- POST /api/admin/users/{id}/deactivate
- POST /api/admin/users/{id}/reset-quota
- GET /api/admin/usage/{id}
- GET /api/admin/users/{id}/heartbeat-logs
- Admin stats and channel config
"""

from __future__ import annotations

import datetime as _dt
import json
import uuid
from collections.abc import AsyncGenerator
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.auth.admin_dep import get_current_admin
from backend.app.auth.dependencies import get_current_user
from backend.app.middleware.rate_limit import _auth_rate_limiter
from backend.app.models import (
    ChannelRoute,
    ChatSession,
    HeartbeatLog,
    IdempotencyKey,
    LLMUsageLog,
    MemoryDocument,
    Message,
    StagedMedia,
    Subscription,
    ToolConfig,
    UsageQuota,
    User,
    UserPermissionSet,
)


@pytest.fixture(autouse=True)
def _clear_rate_limits() -> None:
    _auth_rate_limiter.reset()


async def _create_user_async(
    async_db: async_sessionmaker,
    *,
    user_id: str,
    is_active: bool = True,
    onboarding_complete: bool = True,
    created_at: _dt.datetime | None = None,
    data_sharing_consent: bool = False,
    data_sharing_consent_at: _dt.datetime | None = None,
) -> str:
    """Insert a User row through the async per-test connection.

    Returns the new user's PK.
    """
    new_id = str(uuid.uuid4())
    async with async_db() as db:
        user = User(
            id=new_id,
            user_id=user_id,
            phone="",
            onboarding_complete=onboarding_complete,
            is_active=is_active,
            data_sharing_consent=data_sharing_consent,
            data_sharing_consent_at=data_sharing_consent_at,
        )
        db.add(user)
        await db.commit()
        if created_at is not None:
            # Update created_at after insert because the column has a
            # server default that overrides the assignment-on-create.
            user.created_at = created_at
            db.add(user)
            await db.commit()
    return new_id


async def _add_subscription(
    async_db: async_sessionmaker,
    *,
    user_id: str,
    role: str = "user",
    plan: str = "free",
    status: str = "active",
    email: str = "",
) -> None:
    """Insert a Subscription row through the per-test async connection."""
    async with async_db() as db:
        sub = Subscription(
            user_id=user_id,
            role=role,
            plan=plan,
            status=status,
            email=email,
        )
        db.add(sub)
        await db.commit()


async def _add_quota(
    async_db: async_sessionmaker,
    *,
    user_id: str,
    messages_used: int = 0,
    tokens_used: int = 0,
) -> None:
    """Insert a UsageQuota row for the current month."""
    now = _dt.datetime.now(_dt.UTC)
    period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    async with async_db() as db:
        quota = UsageQuota(
            user_id=user_id,
            period_start=period_start,
            messages_used=messages_used,
            messages_limit=1000,
            tokens_used=tokens_used,
            tokens_limit=1_000_000,
        )
        db.add(quota)
        await db.commit()


@pytest_asyncio.fixture
async def async_admin_user(async_db: async_sessionmaker) -> User:
    """Insert an admin User into the async per-test connection.

    The admin auth dep (``get_current_admin``) is overridden in the
    fixtures below to return this user, so we do not need to materialize
    a matching ``Subscription`` row through the async path. Tests that
    exercise the access-control branch use a separate fixture that
    overrides ``get_current_admin`` to raise 403.
    """
    new_id = str(uuid.uuid4())
    async with async_db() as db:
        admin = User(
            id=new_id,
            user_id="async-admin-user",
            phone="",
            channel_identifier="async-admin-channel",
            preferred_channel="telegram",
            onboarding_complete=True,
        )
        db.add(admin)
        await db.commit()
        await db.refresh(admin)
        db.expunge(admin)
    return admin


@pytest_asyncio.fixture
async def admin_async_client(
    async_db: async_sessionmaker,
    async_admin_user: User,
) -> AsyncGenerator[httpx.AsyncClient]:
    """Async client with admin auth bypassed (always returns admin)."""
    from tests.multi_user.conftest import MULTI_USER_APP as app

    app.dependency_overrides[get_current_user] = lambda: async_admin_user
    app.dependency_overrides[get_current_admin] = lambda: async_admin_user
    mock_manager = MagicMock()
    mock_manager.start_all = AsyncMock(return_value=[])
    mock_manager.stop_all = AsyncMock()
    settings_store_mock = MagicMock()
    settings_store_mock.load = AsyncMock(return_value={})
    settings_store_mock.save = AsyncMock()
    settings_store_mock.delete = AsyncMock()
    with (
        patch("backend.app.main.get_settings_store", return_value=settings_store_mock),
        # The admin router imports ``get_settings_store`` directly from
        # ``backend.app.config_store``; patching just the lifespan-side
        # binding leaves the route writing through the real
        # ``DbSettingsStore`` whose INSERTs FK on ``users.id``. The admin
        # user lives in the per-test ``async_db`` transaction and is
        # invisible to a fresh DB session, which surfaces as an FK
        # violation for any test that PUTs ``/channels/config`` or
        # ``/llm/config``. Mock the route-side binding too.
        patch(
            "backend.app.routers.admin.get_settings_store",
            return_value=settings_store_mock,
        ),
        patch("backend.app.main.import_legacy_config_json", new_callable=AsyncMock),
        patch("backend.app.main.apply_to_settings", return_value={}),
        patch("backend.app.main.load_dotenv"),
        patch("backend.app.main._verify_llm_settings", new_callable=AsyncMock),
        patch("backend.app.main.heartbeat_scheduler"),
        patch("backend.app.main.oauth_refresh_scheduler"),
        patch("backend.app.main.get_manager", return_value=mock_manager),
    ):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client

    app.dependency_overrides.pop(get_current_admin, None)
    app.dependency_overrides.pop(get_current_user, None)


@pytest_asyncio.fixture
async def non_admin_async_client(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> AsyncGenerator[httpx.AsyncClient]:
    """Async client with the admin auth dep simulating a 403."""
    from fastapi import HTTPException

    from tests.multi_user.conftest import MULTI_USER_APP as app

    def _deny() -> User:
        raise HTTPException(status_code=403, detail="Admin access required")

    app.dependency_overrides[get_current_user] = lambda: async_test_user
    app.dependency_overrides[get_current_admin] = _deny
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
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client

    app.dependency_overrides.pop(get_current_admin, None)
    app.dependency_overrides.pop(get_current_user, None)


class TestAdminAccessControl:
    async def test_non_admin_gets_403(
        self,
        non_admin_async_client: httpx.AsyncClient,
    ) -> None:
        resp = await non_admin_async_client.get("/api/admin/users")
        assert resp.status_code == 403

    async def test_admin_gets_200(
        self,
        admin_async_client: httpx.AsyncClient,
    ) -> None:
        resp = await admin_async_client.get("/api/admin/users")
        assert resp.status_code == 200


class TestUserManagement:
    async def test_list_users(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_admin_user: User,
    ) -> None:
        await _add_subscription(
            async_db,
            user_id=async_admin_user.id,
            role="admin",
            email="admin@example.com",
        )
        resp = await admin_async_client.get("/api/admin/users")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        assert len(data["items"]) >= 1
        item = data["items"][0]
        assert "id" in item
        assert "plan" in item

    async def test_list_users_includes_email(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_admin_user: User,
    ) -> None:
        """User list must include email from subscription for admin display."""
        await _add_subscription(
            async_db,
            user_id=async_admin_user.id,
            role="admin",
            email="admin@example.com",
        )
        resp = await admin_async_client.get("/api/admin/users")
        assert resp.status_code == 200
        items = resp.json()["items"]
        match = [i for i in items if i["id"] == async_admin_user.id]
        assert len(match) == 1
        assert match[0]["email"] == "admin@example.com"

    async def test_list_users_search(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_admin_user: User,
    ) -> None:
        await _add_subscription(
            async_db, user_id=async_admin_user.id, role="admin", email="admin@example.com"
        )
        resp = await admin_async_client.get("/api/admin/users?search=async-admin")
        assert resp.status_code == 200
        assert resp.json()["total"] >= 1

        resp = await admin_async_client.get("/api/admin/users?search=nonexistent_xyz")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    async def test_list_users_search_by_email(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_admin_user: User,
    ) -> None:
        """Admin search must match against user email, not just user_id."""
        await _add_subscription(
            async_db,
            user_id=async_admin_user.id,
            role="admin",
            email="searchable@example.com",
        )
        resp = await admin_async_client.get("/api/admin/users?search=searchable@example")
        assert resp.status_code == 200
        assert resp.json()["total"] >= 1

    async def test_list_users_pagination(
        self,
        admin_async_client: httpx.AsyncClient,
    ) -> None:
        resp = await admin_async_client.get("/api/admin/users?offset=0&limit=1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["offset"] == 0
        assert data["limit"] == 1

    async def test_list_users_returns_role_and_metadata(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_admin_user: User,
    ) -> None:
        """UserItem must include role, created_at, last_message_at, and messages_this_month."""
        await _add_subscription(
            async_db, user_id=async_admin_user.id, role="admin", email="admin@example.com"
        )
        resp = await admin_async_client.get("/api/admin/users")
        assert resp.status_code == 200
        items = resp.json()["items"]
        match = next(i for i in items if i["id"] == async_admin_user.id)
        assert match["role"] == "admin"
        assert "created_at" in match
        assert "last_login_at" in match
        assert "last_message_at" in match
        assert match["messages_this_month"] == 0

    async def test_list_users_sort_recent(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_admin_user: User,
    ) -> None:
        """sort=recent puts the most recently created user first."""
        older_id = await _create_user_async(
            async_db,
            user_id="google_oldest",
            created_at=_dt.datetime.now(_dt.UTC) - _dt.timedelta(days=400),
        )
        resp = await admin_async_client.get("/api/admin/users?sort=recent")
        assert resp.status_code == 200
        ids = [i["id"] for i in resp.json()["items"]]
        # The older user should not be the first entry
        assert ids.index(async_admin_user.id) < ids.index(older_id)

    async def test_list_users_sort_oldest(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_admin_user: User,
    ) -> None:
        """sort=oldest reverses recent order."""
        older_id = await _create_user_async(
            async_db,
            user_id="google_old2",
            created_at=_dt.datetime.now(_dt.UTC) - _dt.timedelta(days=400),
        )
        resp = await admin_async_client.get("/api/admin/users?sort=oldest")
        assert resp.status_code == 200
        ids = [i["id"] for i in resp.json()["items"]]
        assert ids.index(older_id) < ids.index(async_admin_user.id)

    async def test_list_users_sort_email(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_admin_user: User,
    ) -> None:
        """sort=email orders by subscription email A->Z, case-insensitive."""
        await _add_subscription(
            async_db, user_id=async_admin_user.id, role="admin", email="zzz@example.com"
        )
        other_id = await _create_user_async(async_db, user_id="google_aaa")
        await _add_subscription(async_db, user_id=other_id, email="aaa@example.com")

        resp = await admin_async_client.get("/api/admin/users?sort=email")
        assert resp.status_code == 200
        ids = [i["id"] for i in resp.json()["items"]]
        assert ids.index(other_id) < ids.index(async_admin_user.id)

    async def test_list_users_last_message_at_reflects_latest_session_activity(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_admin_user: User,
    ) -> None:
        """last_message_at on UserItem comes from MAX(ChatSession.last_message_at)."""
        recent = _dt.datetime(2026, 4, 20, 12, 0, tzinfo=_dt.UTC)
        async with async_db() as db:
            session = ChatSession(
                session_id=f"sess-{uuid.uuid4().hex[:8]}",
                user_id=async_admin_user.id,
                channel="imessage",
                last_message_at=recent,
            )
            db.add(session)
            await db.commit()

        resp = await admin_async_client.get("/api/admin/users")
        assert resp.status_code == 200
        match = next(i for i in resp.json()["items"] if i["id"] == async_admin_user.id)
        assert match["last_message_at"] is not None
        assert match["last_message_at"].startswith("2026-04-20T12:00:00")

    async def test_list_users_sort_last_message(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_admin_user: User,
    ) -> None:
        """sort=last_message orders users by most recent ChatSession.last_message_at desc."""
        # admin user gets a stale session.
        async with async_db() as db:
            db.add(
                ChatSession(
                    session_id=f"sess-{uuid.uuid4().hex[:8]}",
                    user_id=async_admin_user.id,
                    channel="imessage",
                    last_message_at=_dt.datetime(2026, 1, 1, tzinfo=_dt.UTC),
                )
            )
            await db.commit()

        # A second user gets a fresh session (more recent activity).
        recent_id = await _create_user_async(async_db, user_id="google_recent_msg")
        async with async_db() as db:
            db.add(
                ChatSession(
                    session_id=f"sess-{uuid.uuid4().hex[:8]}",
                    user_id=recent_id,
                    channel="imessage",
                    last_message_at=_dt.datetime(2026, 4, 20, tzinfo=_dt.UTC),
                )
            )
            await db.commit()

        resp = await admin_async_client.get("/api/admin/users?sort=last_message")
        assert resp.status_code == 200
        ids = [i["id"] for i in resp.json()["items"]]
        assert ids.index(recent_id) < ids.index(async_admin_user.id)

    async def test_list_users_sort_invalid_returns_400(
        self,
        admin_async_client: httpx.AsyncClient,
    ) -> None:
        """Invalid sort param raises 400, not silent fallback."""
        resp = await admin_async_client.get("/api/admin/users?sort=garbage")
        assert resp.status_code == 400
        assert "garbage" in resp.json()["detail"]

    async def test_list_users_returns_consent_fields(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_admin_user: User,
    ) -> None:
        """UserItem must surface data_sharing_consent, consent_at, conversation_count."""
        async with async_db() as db:
            u = (await db.execute(select(User).where(User.id == async_admin_user.id))).scalar_one()
            u.data_sharing_consent = True
            u.data_sharing_consent_at = _dt.datetime.now(_dt.UTC)
            db.add(
                ChatSession(
                    user_id=async_admin_user.id,
                    session_id=f"sess-{uuid.uuid4().hex[:8]}",
                    channel="imessage",
                )
            )
            await db.commit()

        resp = await admin_async_client.get("/api/admin/users")
        assert resp.status_code == 200
        match = next(i for i in resp.json()["items"] if i["id"] == async_admin_user.id)
        assert match["data_sharing_consent"] is True
        assert match["data_sharing_consent_at"] is not None
        assert match["conversation_count"] == 1

    async def test_list_users_consent_filter_shared(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_admin_user: User,
    ) -> None:
        """consent=shared returns only users with data_sharing_consent=True."""
        async with async_db() as db:
            u = (await db.execute(select(User).where(User.id == async_admin_user.id))).scalar_one()
            u.data_sharing_consent = True
            u.data_sharing_consent_at = _dt.datetime.now(_dt.UTC)
            await db.commit()
        non_consent_id = await _create_user_async(async_db, user_id="google_no_consent")
        await _add_subscription(async_db, user_id=non_consent_id, email="silent@example.com")

        resp = await admin_async_client.get("/api/admin/users?consent=shared")
        assert resp.status_code == 200
        ids = {i["id"] for i in resp.json()["items"]}
        assert async_admin_user.id in ids
        assert non_consent_id not in ids

    async def test_list_users_consent_filter_none(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_admin_user: User,
    ) -> None:
        """consent=none excludes consenting users."""
        async with async_db() as db:
            u = (await db.execute(select(User).where(User.id == async_admin_user.id))).scalar_one()
            u.data_sharing_consent = True
            u.data_sharing_consent_at = _dt.datetime.now(_dt.UTC)
            await db.commit()
        non_consent_id = await _create_user_async(async_db, user_id="google_no_consent2")
        await _add_subscription(async_db, user_id=non_consent_id, email="silent2@example.com")

        resp = await admin_async_client.get("/api/admin/users?consent=none")
        assert resp.status_code == 200
        ids = {i["id"] for i in resp.json()["items"]}
        assert async_admin_user.id not in ids
        assert non_consent_id in ids

    async def test_list_users_consent_invalid_returns_400(
        self,
        admin_async_client: httpx.AsyncClient,
    ) -> None:
        resp = await admin_async_client.get("/api/admin/users?consent=garbage")
        assert resp.status_code == 400
        assert "garbage" in resp.json()["detail"]

    async def test_list_users_sort_consent(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_admin_user: User,
    ) -> None:
        """sort=consent puts most recently consented users first; non-consenters last."""
        # admin user consented today
        async with async_db() as db:
            u = (await db.execute(select(User).where(User.id == async_admin_user.id))).scalar_one()
            u.data_sharing_consent = True
            u.data_sharing_consent_at = _dt.datetime.now(_dt.UTC)
            await db.commit()

        older_id = await _create_user_async(
            async_db,
            user_id="google_old_consent",
            data_sharing_consent=True,
            data_sharing_consent_at=_dt.datetime.now(_dt.UTC) - _dt.timedelta(days=30),
        )
        await _add_subscription(async_db, user_id=older_id, email="old@example.com")

        non_consent_id = await _create_user_async(async_db, user_id="google_no_consent3")
        await _add_subscription(async_db, user_id=non_consent_id, email="silent3@example.com")

        resp = await admin_async_client.get("/api/admin/users?sort=consent")
        assert resp.status_code == 200
        ids = [i["id"] for i in resp.json()["items"]]
        assert ids.index(async_admin_user.id) < ids.index(older_id)
        assert ids.index(older_id) < ids.index(non_consent_id)

    async def test_activate_user(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
    ) -> None:
        other_id = await _create_user_async(async_db, user_id="google_other", is_active=False)

        resp = await admin_async_client.post(f"/api/admin/users/{other_id}/activate")
        assert resp.status_code == 200
        assert resp.json()["is_active"] is True

    async def test_activate_self_blocked(
        self,
        admin_async_client: httpx.AsyncClient,
        async_admin_user: User,
    ) -> None:
        """Admin cannot activate their own account."""
        resp = await admin_async_client.post(f"/api/admin/users/{async_admin_user.id}/activate")
        assert resp.status_code == 400
        assert "cannot activate themselves" in resp.json()["detail"].lower()

    async def test_deactivate_user(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
    ) -> None:
        other_id = await _create_user_async(async_db, user_id="google_deact", is_active=True)

        resp = await admin_async_client.post(f"/api/admin/users/{other_id}/deactivate")
        assert resp.status_code == 200
        assert resp.json()["is_active"] is False

    async def test_deactivate_self_blocked(
        self,
        admin_async_client: httpx.AsyncClient,
        async_admin_user: User,
    ) -> None:
        """Admin cannot deactivate their own account."""
        resp = await admin_async_client.post(f"/api/admin/users/{async_admin_user.id}/deactivate")
        assert resp.status_code == 400
        assert "cannot deactivate themselves" in resp.json()["detail"].lower()

    async def test_activate_nonexistent(
        self,
        admin_async_client: httpx.AsyncClient,
    ) -> None:
        resp = await admin_async_client.post(
            "/api/admin/users/00000000-0000-0000-0000-000000000000/activate"
        )
        assert resp.status_code == 404

    async def test_reset_quota(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
    ) -> None:
        other_id = await _create_user_async(async_db, user_id="google_reset", is_active=True)
        await _add_quota(async_db, user_id=other_id, messages_used=10)

        resp = await admin_async_client.post(f"/api/admin/users/{other_id}/reset-quota")
        assert resp.status_code == 200
        data = resp.json()
        assert data["messages"]["used"] == 0

    async def test_reset_quota_self_blocked(
        self,
        admin_async_client: httpx.AsyncClient,
        async_admin_user: User,
    ) -> None:
        """Admin cannot reset their own quota."""
        resp = await admin_async_client.post(f"/api/admin/users/{async_admin_user.id}/reset-quota")
        assert resp.status_code == 400
        assert "cannot reset their own quota" in resp.json()["detail"].lower()


class TestCompactUserContext:
    """Admin POST /api/admin/users/{id}/compact-now.

    The endpoint delegates to OSS ``admin_compact_visible_messages``. We
    mock that function so these tests exercise only the premium-side
    plumbing: route registration, body validation, audit-log detail,
    user-not-found 404, and that the response surfaces the OSS result.
    """

    async def test_returns_404_for_unknown_user(
        self,
        admin_async_client: httpx.AsyncClient,
    ) -> None:
        resp = await admin_async_client.post(
            "/api/admin/users/00000000-0000-0000-0000-000000000000/compact-now",
            json={},
        )
        assert resp.status_code == 404

    async def test_compacts_with_default_body(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
    ) -> None:
        """Empty body runs the OSS helper with default args and surfaces
        every field of the OSS result on the response.
        """
        other_id = await _create_user_async(async_db, user_id="google_compact", is_active=True)
        from backend.app.agent.context import AdminCompactionResult

        oss_result = AdminCompactionResult(
            compacted_message_count=7,
            new_watermark=12,
            memory_updated=True,
            event_id=42,
        )
        with patch(
            "backend.app.routers.admin.admin_compact_visible_messages",
            new=AsyncMock(return_value=oss_result),
        ) as mock_compact:
            resp = await admin_async_client.post(
                f"/api/admin/users/{other_id}/compact-now",
                json={},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data == {
            "compacted_message_count": 7,
            "new_watermark": 12,
            "memory_updated": True,
            "event_id": 42,
            "previous_event_id": None,
        }
        mock_compact.assert_awaited_once()
        kwargs = mock_compact.call_args.kwargs
        assert kwargs == {"keep_recent": 0, "admin_note": None}
        # The user_id is the positional arg.
        args = mock_compact.call_args.args
        assert args == (other_id,)

    async def test_no_op_response_surfaces_previous_event_id(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
    ) -> None:
        """A no-op call must surface ``previous_event_id`` so the admin can
        tell apart "you already did this" from "there was nothing to do".
        Regression for issue mozilla-ai/clawbolt#1291.
        """
        other_id = await _create_user_async(async_db, user_id="google_compact2", is_active=True)
        from backend.app.agent.context import AdminCompactionResult

        oss_result = AdminCompactionResult(
            compacted_message_count=0,
            new_watermark=12,
            memory_updated=False,
            event_id=None,
            previous_event_id=42,
        )
        with patch(
            "backend.app.routers.admin.admin_compact_visible_messages",
            new=AsyncMock(return_value=oss_result),
        ):
            resp = await admin_async_client.post(
                f"/api/admin/users/{other_id}/compact-now",
                json={},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["event_id"] is None
        assert data["previous_event_id"] == 42

    async def test_passes_keep_recent_and_hint_through(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
    ) -> None:
        """``keep_recent`` and ``hint`` body fields must reach the OSS
        helper, not be silently dropped at the schema boundary.
        """
        other_id = await _create_user_async(async_db, user_id="google_compact3", is_active=True)
        from backend.app.agent.context import AdminCompactionResult

        oss_result = AdminCompactionResult(
            compacted_message_count=3,
            new_watermark=8,
            memory_updated=False,
            event_id=99,
        )
        with patch(
            "backend.app.routers.admin.admin_compact_visible_messages",
            new=AsyncMock(return_value=oss_result),
        ) as mock_compact:
            resp = await admin_async_client.post(
                f"/api/admin/users/{other_id}/compact-now",
                json={
                    "keep_recent": 2,
                    "hint": "ignore prior agent self-claims about AppFolio",
                },
            )
        assert resp.status_code == 200
        kwargs = mock_compact.call_args.kwargs
        assert kwargs["keep_recent"] == 2
        assert kwargs["admin_note"] == "ignore prior agent self-claims about AppFolio"

    async def test_rejects_negative_keep_recent(
        self,
        admin_async_client: httpx.AsyncClient,
        async_admin_user: User,
    ) -> None:
        """The schema's ``ge=0`` constraint must reject a negative
        ``keep_recent`` at the boundary, before the OSS helper runs.
        """
        with patch(
            "backend.app.routers.admin.admin_compact_visible_messages",
            new=AsyncMock(),
        ) as mock_compact:
            resp = await admin_async_client.post(
                f"/api/admin/users/{async_admin_user.id}/compact-now",
                json={"keep_recent": -1},
            )
        assert resp.status_code == 422
        mock_compact.assert_not_awaited()

    async def test_compact_self_blocked(
        self,
        admin_async_client: httpx.AsyncClient,
        async_admin_user: User,
    ) -> None:
        """Admin cannot compact their own context."""
        resp = await admin_async_client.post(
            f"/api/admin/users/{async_admin_user.id}/compact-now",
            json={},
        )
        assert resp.status_code == 400
        assert "cannot compact their own context" in resp.json()["detail"].lower()


class TestHygieneCompactMemory:
    """Admin POST /api/admin/users/{id}/hygiene-compact-memory.

    The endpoint delegates to OSS ``hygiene_compact_memory``. These tests
    exercise the premium-side plumbing: route registration, user-not-found
    404, audit-log detail, and that the response surfaces the OSS result.
    """

    async def test_returns_404_for_unknown_user(
        self,
        admin_async_client: httpx.AsyncClient,
    ) -> None:
        resp = await admin_async_client.post(
            "/api/admin/users/00000000-0000-0000-0000-000000000000/hygiene-compact-memory",
        )
        assert resp.status_code == 404

    async def test_hygiene_cleans_memory(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
    ) -> None:
        """A hygiene run returns memory_updated=True and the memory_text
        when the OSS helper reports a change.
        """
        other_id = await _create_user_async(async_db, user_id="google_hygiene1", is_active=True)
        with patch(
            "backend.app.routers.admin.hygiene_compact_memory",
            new=AsyncMock(
                return_value=(
                    "## Pricing\n- Standard day rate: $600\n",
                    True,
                )
            ),
        ) as mock_hygiene:
            resp = await admin_async_client.post(
                f"/api/admin/users/{other_id}/hygiene-compact-memory",
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data == {
            "memory_updated": True,
            "memory_text": "## Pricing\n- Standard day rate: $600\n",
        }
        mock_hygiene.assert_awaited_once()
        args = mock_hygiene.call_args.args
        assert args == (other_id,)

    async def test_hygiene_no_change(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
    ) -> None:
        """When the OSS helper returns no change, the endpoint surfaces
        memory_updated=False and empty memory_text.
        """
        other_id = await _create_user_async(async_db, user_id="google_hygiene2", is_active=True)
        with patch(
            "backend.app.routers.admin.hygiene_compact_memory",
            new=AsyncMock(return_value=("", False)),
        ) as mock_hygiene:
            resp = await admin_async_client.post(
                f"/api/admin/users/{other_id}/hygiene-compact-memory",
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data == {
            "memory_updated": False,
            "memory_text": "",
        }
        mock_hygiene.assert_awaited_once()

    async def test_hygiene_compact_self_blocked(
        self,
        admin_async_client: httpx.AsyncClient,
        async_admin_user: User,
    ) -> None:
        """Admin cannot hygiene-compact their own memory."""
        resp = await admin_async_client.post(
            f"/api/admin/users/{async_admin_user.id}/hygiene-compact-memory",
        )
        assert resp.status_code == 400
        assert "cannot hygiene-compact their own memory" in resp.json()["detail"].lower()


class TestPurgeUser:
    """Admin DELETE /api/admin/users/{id} (issue #395 async pilot).

    Routes the request through ``async_client`` because ``purge_user`` now
    depends on ``Depends(get_async_db)``. Setup goes through ``async_db()``
    so target rows are visible to the route's per-test connection. The
    admin auth check (``get_current_admin``) is overridden to a fixed
    User so tests do not have to round-trip the sync ``Subscription`` row
    that the cross-API connection split would hide from the async route.
    """

    async def test_delete_user(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
    ) -> None:
        """Admin can hard-delete another user, removing their row entirely."""
        target_id = await _create_user_async(async_db, user_id="google_purge_me")

        resp = await admin_async_client.delete(f"/api/admin/users/{target_id}")

        assert resp.status_code == 200
        assert resp.json()["status"] == "purged"
        async with async_db() as db:
            row = (await db.execute(select(User).where(User.id == target_id))).scalar_one_or_none()
        assert row is None

    async def test_delete_user_nonexistent(
        self,
        admin_async_client: httpx.AsyncClient,
    ) -> None:
        resp = await admin_async_client.delete(
            "/api/admin/users/00000000-0000-0000-0000-000000000000"
        )
        assert resp.status_code == 404

    async def test_delete_user_self_blocked(
        self,
        admin_async_client: httpx.AsyncClient,
        async_admin_user: User,
    ) -> None:
        """Admin cannot purge their own account via this endpoint."""
        resp = await admin_async_client.delete(f"/api/admin/users/{async_admin_user.id}")
        assert resp.status_code == 400
        assert "themselves" in resp.json()["detail"].lower()

    async def test_delete_user_non_admin_blocked(
        self,
        non_admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
    ) -> None:
        target_id = await _create_user_async(async_db, user_id="google_target")

        resp = await non_admin_async_client.delete(f"/api/admin/users/{target_id}")
        assert resp.status_code == 403

    async def test_delete_user_double_purge_returns_404(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
    ) -> None:
        """Second DELETE for the same user should 404, not 500."""
        target_id = await _create_user_async(async_db, user_id="google_twice")

        r1 = await admin_async_client.delete(f"/api/admin/users/{target_id}")
        r2 = await admin_async_client.delete(f"/api/admin/users/{target_id}")

        assert r1.status_code == 200
        assert r2.status_code == 404


class TestUserDetail:
    async def test_returns_404_for_unknown_user(
        self,
        admin_async_client: httpx.AsyncClient,
    ) -> None:
        resp = await admin_async_client.get(f"/api/admin/users/{uuid.uuid4()}")
        assert resp.status_code == 404

    async def test_empty_user_returns_identity_and_empty_lists(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_admin_user: User,
    ) -> None:
        await _add_subscription(
            async_db, user_id=async_admin_user.id, role="admin", email="admin@example.com"
        )
        resp = await admin_async_client.get(f"/api/admin/users/{async_admin_user.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == async_admin_user.id
        assert data["email"] == "admin@example.com"
        assert data["plan"] == "free"
        assert data["role"] == "admin"
        assert data["subscription_created_at"] is not None
        # Issue #325 work item 2: content is no longer returned.
        for dropped in (
            "recent_messages",
            "recent_tool_calls",
            "soul_text",
            "user_text",
            "heartbeat_text",
            "memory_text",
            "history_text",
            "memory_text_truncated",
            "history_text_truncated",
        ):
            assert dropped not in data, f"{dropped!r} must not be in slim admin response"

    async def test_response_drops_content_fields_even_when_data_exists(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_admin_user: User,
    ) -> None:
        """Negative test: even with messages, memory, and profile content
        present in the DB, the slim response must not include any of it."""
        async with async_db() as db:
            session = ChatSession(
                session_id=f"sess-{uuid.uuid4().hex[:8]}",
                user_id=async_admin_user.id,
                channel="imessage",
            )
            db.add(session)
            await db.flush()
            db.add(
                Message(
                    session_id=session.id,
                    seq=1,
                    direction="inbound",
                    body="this body must not surface in the admin response",
                )
            )
            user_in_db = (
                await db.execute(select(User).where(User.id == async_admin_user.id))
            ).scalar_one()
            user_in_db.soul_text = "soul-content-must-not-leak"
            user_in_db.user_text = "user-text-must-not-leak"
            user_in_db.heartbeat_text = "heartbeat-text-must-not-leak"
            db.add(
                MemoryDocument(
                    user_id=async_admin_user.id,
                    memory_text="memory-must-not-leak",
                    history_text="history-must-not-leak",
                )
            )
            await db.commit()

        resp = await admin_async_client.get(f"/api/admin/users/{async_admin_user.id}")
        assert resp.status_code == 200
        body_text = resp.text
        header_text = "\n".join(f"{k}: {v}" for k, v in resp.headers.items())
        markers = (
            "this body must not surface in the admin response",
            "soul-content-must-not-leak",
            "user-text-must-not-leak",
            "heartbeat-text-must-not-leak",
            "memory-must-not-leak",
            "history-must-not-leak",
        )
        for marker in markers:
            assert marker not in body_text, f"{marker!r} leaked into the slim admin response body"
            assert marker not in header_text, (
                f"{marker!r} leaked into the slim admin response headers"
            )

    async def test_500_path_does_not_leak_content(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_admin_user: User,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Even when the route blows up mid-serialization, error responses
        must not echo content fields."""
        marker = "five-hundred-marker-db77"
        async with async_db() as db:
            user_in_db = (
                await db.execute(select(User).where(User.id == async_admin_user.id))
            ).scalar_one()
            user_in_db.soul_text = marker
            db.add(MemoryDocument(user_id=async_admin_user.id, memory_text=marker, history_text=""))
            await db.commit()

        from backend.app import schemas

        original_init = schemas.AdminUserDetailResponse.__init__

        def kaboom_init(self: object, **_kwargs: object) -> None:
            raise RuntimeError(f"simulated serialization explosion {marker}")

        monkeypatch.setattr(schemas.AdminUserDetailResponse, "__init__", kaboom_init)

        try:
            resp = await admin_async_client.get(f"/api/admin/users/{async_admin_user.id}")
        finally:
            monkeypatch.setattr(schemas.AdminUserDetailResponse, "__init__", original_init)

        assert marker not in resp.text, f"{marker!r} leaked via error-path response body"
        header_text = "\n".join(f"{k}: {v}" for k, v in resp.headers.items())
        assert marker not in header_text, f"{marker!r} leaked via error-path response headers"

    async def test_non_admin_blocked(
        self,
        non_admin_async_client: httpx.AsyncClient,
        async_test_user: User,
    ) -> None:
        resp = await non_admin_async_client.get(f"/api/admin/users/{async_test_user.id}")
        assert resp.status_code == 403

    async def test_separate_non_admin_caller_cannot_inspect_other_user(
        self,
        non_admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
    ) -> None:
        """A non-admin caller (different user) must not be able to inspect."""
        other_id = await _create_user_async(async_db, user_id="other_user")
        resp = await non_admin_async_client.get(f"/api/admin/users/{other_id}")
        assert resp.status_code == 403

    async def test_returns_profile_config_fields(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_admin_user: User,
    ) -> None:
        """Profile *config* fields survive the slim. Content does not."""
        async with async_db() as db:
            user_in_db = (
                await db.execute(select(User).where(User.id == async_admin_user.id))
            ).scalar_one()
            user_in_db.timezone = "America/Los_Angeles"
            user_in_db.preferred_channel = "sms"
            user_in_db.heartbeat_opt_in = True
            user_in_db.heartbeat_frequency = "30m"
            await db.commit()

        resp = await admin_async_client.get(f"/api/admin/users/{async_admin_user.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["timezone"] == "America/Los_Angeles"
        assert data["preferred_channel"] == "sms"
        assert data["heartbeat_opt_in"] is True
        assert data["heartbeat_frequency"] == "30m"

    async def test_empty_integrations(
        self,
        admin_async_client: httpx.AsyncClient,
        async_admin_user: User,
    ) -> None:
        """A user with no tool configs / channel routes gets empty lists."""
        resp = await admin_async_client.get(f"/api/admin/users/{async_admin_user.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["tool_configs"] == []
        assert data["channel_routes"] == []

    async def test_tool_configs_sorted_by_name(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_admin_user: User,
    ) -> None:
        """Tool configs come back sorted alphabetically by tool_name."""
        async with async_db() as db:
            for name in ("quickbooks", "calendar", "companycam"):
                db.add(ToolConfig(user_id=async_admin_user.id, name=name, enabled=True))
            await db.commit()

        resp = await admin_async_client.get(f"/api/admin/users/{async_admin_user.id}")
        assert resp.status_code == 200
        names = [t["tool_name"] for t in resp.json()["tool_configs"]]
        assert names == ["calendar", "companycam", "quickbooks"]

    async def test_permissions_default_when_no_row(
        self,
        admin_async_client: httpx.AsyncClient,
        async_admin_user: User,
    ) -> None:
        """A user with no UserPermissionSet row gets empty permission lists."""
        resp = await admin_async_client.get(f"/api/admin/users/{async_admin_user.id}")
        assert resp.status_code == 200
        permissions = resp.json()["permissions"]
        assert permissions == {"tools": [], "resources": []}

    async def test_permissions_returns_tools_and_resources(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_admin_user: User,
    ) -> None:
        """Tool and resource permission overrides surface in the admin detail."""
        async with async_db() as db:
            db.add(
                UserPermissionSet(
                    user_id=async_admin_user.id,
                    data=json.dumps(
                        {
                            "version": 1,
                            "tools": {
                                "web_search": "always",
                                "bash_exec": "deny",
                                "calendar_create": "ask",
                            },
                            "resources": {
                                "web_fetch": {
                                    "homedepot.com": "always",
                                    "*.gov": "always",
                                }
                            },
                        }
                    ),
                )
            )
            await db.commit()

        resp = await admin_async_client.get(f"/api/admin/users/{async_admin_user.id}")
        assert resp.status_code == 200
        permissions = resp.json()["permissions"]

        # Tools come back sorted by tool_name with their stored levels.
        assert permissions["tools"] == [
            {"tool_name": "bash_exec", "level": "deny"},
            {"tool_name": "calendar_create", "level": "ask"},
            {"tool_name": "web_search", "level": "always"},
        ]

        # Resources are flattened into (tool_name, resource, level) rows
        # so the admin UI can render them as a table.
        assert permissions["resources"] == [
            {"tool_name": "web_fetch", "resource": "*.gov", "level": "always"},
            {"tool_name": "web_fetch", "resource": "homedepot.com", "level": "always"},
        ]

    async def test_permissions_skips_invalid_entries(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_admin_user: User,
    ) -> None:
        """Non-string levels or malformed sub-objects are dropped, not echoed."""
        async with async_db() as db:
            db.add(
                UserPermissionSet(
                    user_id=async_admin_user.id,
                    data=json.dumps(
                        {
                            "version": 1,
                            "tools": {
                                "ok_tool": "always",
                                "broken_tool": 42,  # non-string level
                            },
                            "resources": {
                                "web_fetch": "not-a-dict",  # malformed sub-map
                                "good_tool": {"a.com": "ask"},
                            },
                        }
                    ),
                )
            )
            await db.commit()

        resp = await admin_async_client.get(f"/api/admin/users/{async_admin_user.id}")
        assert resp.status_code == 200
        permissions = resp.json()["permissions"]
        assert permissions["tools"] == [{"tool_name": "ok_tool", "level": "always"}]
        assert permissions["resources"] == [
            {"tool_name": "good_tool", "resource": "a.com", "level": "ask"},
        ]

    async def test_channel_routes_sorted_recent_first_nulls_last(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_admin_user: User,
    ) -> None:
        """Channels come back ordered by last_inbound_at desc; nulls go last."""
        now = _dt.datetime.now(_dt.UTC)
        async with async_db() as db:
            db.add(
                ChannelRoute(
                    user_id=async_admin_user.id,
                    channel="sms",
                    channel_identifier="+15555550001",
                    enabled=True,
                    last_inbound_at=now - _dt.timedelta(minutes=5),
                )
            )
            db.add(
                ChannelRoute(
                    user_id=async_admin_user.id,
                    channel="telegram",
                    channel_identifier="@example",
                    enabled=False,
                    last_inbound_at=None,
                )
            )
            db.add(
                ChannelRoute(
                    user_id=async_admin_user.id,
                    channel="imessage",
                    channel_identifier="user@example.com",
                    enabled=True,
                    last_inbound_at=now - _dt.timedelta(days=2),
                )
            )
            await db.commit()

        resp = await admin_async_client.get(f"/api/admin/users/{async_admin_user.id}")
        assert resp.status_code == 200
        routes = resp.json()["channel_routes"]
        assert [r["channel"] for r in routes] == ["sms", "imessage", "telegram"]
        assert routes[-1]["last_inbound_at"] is None

    async def test_channel_identifiers_are_masked(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_admin_user: User,
    ) -> None:
        """``channel_identifier`` is PII; the slim response masks it."""
        phone = "+19998887777"
        email = "channel-route-marker-9f2a@example.test"
        telegram = "555444333222"
        bot_handle = "@channelroutemarkerbot"
        async with async_db() as db:
            for channel, identifier in (
                ("sms", phone),
                ("imessage", email),
                ("telegram", telegram),
                ("telegram", bot_handle),
            ):
                db.add(
                    ChannelRoute(
                        user_id=async_admin_user.id,
                        channel=channel,
                        channel_identifier=identifier,
                        enabled=True,
                    )
                )
            await db.commit()

        resp = await admin_async_client.get(f"/api/admin/users/{async_admin_user.id}")
        assert resp.status_code == 200
        assert phone not in resp.text, "phone number leaked into the slim admin response"
        assert email not in resp.text, "email leaked into the slim admin response"
        assert telegram not in resp.text, "Telegram chat id leaked into the slim admin response"
        assert bot_handle not in resp.text, "@handle leaked into the slim admin response"

        masked = [r["channel_identifier"] for r in resp.json()["channel_routes"]]
        assert any(m.startswith("+199") and m.endswith("7777") and "·" in m for m in masked)
        assert any(m.startswith("c***@e***") and m.endswith("t**") for m in masked)
        assert any(m.startswith("55") and m.endswith("22") and "·" in m for m in masked)
        assert any(
            m.startswith("@c") and m.endswith("ot") and "·" in m and bot_handle not in m
            for m in masked
        )
        assert "example.test" not in resp.text

    def test_mask_channel_identifier_edge_cases(self) -> None:
        """Direct unit tests for ``_mask_channel_identifier``."""
        from backend.app.routers.admin import _mask_channel_identifier

        masked = _mask_channel_identifier("+1234567")
        assert masked != "+1234567"
        assert "·" in masked
        assert masked.startswith("+") and masked.endswith("67")
        assert "23456" not in masked

        masked = _mask_channel_identifier("+12345678")
        assert masked != "+12345678"
        assert "·" in masked

        masked = _mask_channel_identifier("me@nodot")
        assert "nodot" not in masked
        assert masked.startswith("m") and masked.endswith("ot") and "·" in masked

        assert _mask_channel_identifier("") == ""
        assert _mask_channel_identifier("abc") == "abc"

        masked = _mask_channel_identifier("a‮txt@example.com")
        assert "‮" not in masked

        masked = _mask_channel_identifier("user@acme.local")
        assert "local" not in masked
        assert masked.endswith("l**")

        masked = _mask_channel_identifier("+15555550@example.com")
        assert not masked.startswith("+")
        assert masked.startswith("***@")

        masked = _mask_channel_identifier("@somebot")
        assert masked.startswith("@") and masked.endswith("ot")
        assert masked.count("·") == 5

        big = "A" * 1024
        masked = _mask_channel_identifier(big)
        assert len(masked) <= 128, f"masked output {len(masked)} chars, expected ≤128"

    async def test_writes_audit_log_on_each_call(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_admin_user: User,
    ) -> None:
        """Every call to GET /admin/users/{id} writes an AdminAuditLog row."""
        from backend.app.models import AdminAuditLog

        # After #430 the audit dep is fully async and the audit insert
        # opens its own ``db_session_async()``. Under the ``async_db``
        # per-test rollback fixture both the route session and the
        # audit session share the outer SAVEPOINT; the audit
        # ``commit()`` only releases its inner savepoint into the
        # route's, which is itself rolled back when the route session
        # closes. Coverage for "audit row written for view_user_detail"
        # lives in ``tests/test_admin_audit.py`` (404 path matrix +
        # mutation tests use the TRUNCATE-isolation ``client`` fixture
        # where audit commits land in a real DB row).
        pytest.skip(
            "Audit dep is async post-#430 but the per-test SAVEPOINT pattern "
            "rolls back the audit row when the route session closes; covered "
            "by the TRUNCATE-isolation suite in tests/test_admin_audit.py."
        )
        _ = AdminAuditLog
        _ = admin_async_client
        _ = async_db
        _ = async_admin_user

    async def test_writes_audit_log_for_heartbeat_logs_endpoint(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_admin_user: User,
    ) -> None:
        """Originally tested that GET /admin/users/{id}/heartbeat-logs writes an audit row."""
        pytest.skip(
            "Audit dep is async post-#430 but the per-test SAVEPOINT pattern "
            "rolls back the audit row when the route session closes; covered "
            "by the TRUNCATE-isolation suite in tests/test_admin_audit.py "
            "(404 matrix includes ``view_heartbeat_logs``)."
        )
        _ = admin_async_client
        _ = async_db
        _ = async_admin_user

    async def test_writes_audit_log_for_llm_usage_logs_endpoint(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_admin_user: User,
    ) -> None:
        """Originally tested that GET /admin/users/{id}/llm-usage-logs writes an audit row."""
        pytest.skip(
            "Audit dep is async post-#430 but the per-test SAVEPOINT pattern "
            "rolls back the audit row when the route session closes; covered "
            "by the TRUNCATE-isolation suite in tests/test_admin_audit.py "
            "(404 matrix includes ``view_llm_usage_logs``)."
        )
        _ = admin_async_client
        _ = async_db
        _ = async_admin_user

    async def test_audit_failure_does_not_break_user_detail_read(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_admin_user: User,
    ) -> None:
        """Originally tested fail-open behavior of the audit dep: a failure in
        the audit row insert must not break the user-detail read."""
        pytest.skip(
            "Audit dep is async post-#430; fail-open behaviour is covered by "
            "``tests/test_admin_audit.py::TestMutationSurvivesAuditFailure`` "
            "which exercises the same path on the TRUNCATE-isolation client."
        )
        _ = admin_async_client
        _ = async_db
        _ = async_admin_user


class TestAdminUsage:
    async def test_get_user_usage(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_admin_user: User,
    ) -> None:
        await _add_quota(async_db, user_id=async_admin_user.id)
        resp = await admin_async_client.get(f"/api/admin/usage/{async_admin_user.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert "messages" in data
        assert "tokens" in data
        # AdminUsageSummary surfaces aggregate LLM spend so the admin
        # user-detail page can render it without paging through logs.
        assert data["period_cost_usd"] == "0.000000"
        assert data["lifetime_cost_usd"] == "0.000000"

    async def test_cost_totals_sum_user_logs(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_admin_user: User,
    ) -> None:
        """``period_cost_usd`` and ``lifetime_cost_usd`` aggregate the user's calls."""
        await _add_quota(async_db, user_id=async_admin_user.id)
        async with async_db() as db:
            db.add(
                LLMUsageLog(
                    user_id=async_admin_user.id,
                    provider="anthropic",
                    model="claude-sonnet-4-6",
                    purpose="agent_main",
                    input_tokens=1000,
                    output_tokens=200,
                    total_tokens=1200,
                    cost=Decimal("0.123456"),
                )
            )
            db.add(
                LLMUsageLog(
                    user_id=async_admin_user.id,
                    provider="anthropic",
                    model="claude-sonnet-4-6",
                    purpose="agent_followup",
                    input_tokens=500,
                    output_tokens=50,
                    total_tokens=550,
                    cost=Decimal("0.500000"),
                )
            )
            await db.commit()

        resp = await admin_async_client.get(f"/api/admin/usage/{async_admin_user.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["period_cost_usd"] == "0.623456"
        assert data["lifetime_cost_usd"] == "0.623456"

    async def test_cost_totals_isolate_per_user(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_admin_user: User,
    ) -> None:
        """Another user's LLM calls do not bleed into this user's totals."""
        await _add_quota(async_db, user_id=async_admin_user.id)
        other_id = await _create_user_async(async_db, user_id="google_cost_isolation")
        async with async_db() as db:
            db.add(
                LLMUsageLog(
                    user_id=async_admin_user.id,
                    provider="anthropic",
                    model="claude-sonnet-4-6",
                    purpose="agent_main",
                    input_tokens=100,
                    output_tokens=10,
                    total_tokens=110,
                    cost=Decimal("0.010000"),
                )
            )
            db.add(
                LLMUsageLog(
                    user_id=other_id,
                    provider="anthropic",
                    model="claude-sonnet-4-6",
                    purpose="agent_main",
                    input_tokens=9999,
                    output_tokens=9999,
                    total_tokens=19998,
                    cost=Decimal("99.999999"),
                )
            )
            await db.commit()

        resp = await admin_async_client.get(f"/api/admin/usage/{async_admin_user.id}")
        assert resp.status_code == 200
        assert resp.json()["lifetime_cost_usd"] == "0.010000"

    async def test_cost_totals_split_by_period_boundary(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_admin_user: User,
    ) -> None:
        """A log from a prior month counts toward lifetime but not period."""
        await _add_quota(async_db, user_id=async_admin_user.id)
        now = _dt.datetime.now(_dt.UTC)
        period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        before_period = period_start - _dt.timedelta(hours=1)
        async with async_db() as db:
            old_log = LLMUsageLog(
                user_id=async_admin_user.id,
                provider="anthropic",
                model="claude-sonnet-4-6",
                purpose="agent_main",
                input_tokens=100,
                output_tokens=10,
                total_tokens=110,
                cost=Decimal("1.000000"),
            )
            db.add(old_log)
            await db.commit()
            old_log.created_at = before_period
            db.add(old_log)
            await db.commit()
            db.add(
                LLMUsageLog(
                    user_id=async_admin_user.id,
                    provider="anthropic",
                    model="claude-sonnet-4-6",
                    purpose="agent_main",
                    input_tokens=200,
                    output_tokens=20,
                    total_tokens=220,
                    cost=Decimal("0.250000"),
                )
            )
            await db.commit()

        resp = await admin_async_client.get(f"/api/admin/usage/{async_admin_user.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["period_cost_usd"] == "0.250000"
        assert data["lifetime_cost_usd"] == "1.250000"

    async def test_usage_returns_404_when_user_missing(
        self,
        admin_async_client: httpx.AsyncClient,
    ) -> None:
        resp = await admin_async_client.get("/api/admin/usage/00000000-0000-0000-0000-000000000000")
        assert resp.status_code == 404


class TestHeartbeatLogs:
    async def test_get_heartbeat_logs_empty(
        self,
        admin_async_client: httpx.AsyncClient,
        async_admin_user: User,
    ) -> None:
        resp = await admin_async_client.get(
            f"/api/admin/users/{async_admin_user.id}/heartbeat-logs"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    async def test_get_heartbeat_logs_with_data(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_admin_user: User,
    ) -> None:
        async with async_db() as db:
            for _ in range(3):
                db.add(HeartbeatLog(user_id=async_admin_user.id))
            await db.commit()

        resp = await admin_async_client.get(
            f"/api/admin/users/{async_admin_user.id}/heartbeat-logs"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        assert len(data["items"]) == 3
        for item in data["items"]:
            assert item["user_id"] == async_admin_user.id
            assert "created_at" in item
            assert "id" in item

    async def test_get_heartbeat_logs_returns_metadata_only(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_admin_user: User,
    ) -> None:
        """Issue #325 work item 2: heartbeat content is not in the default response."""
        _has_enriched = hasattr(HeartbeatLog, "action_type")
        kwargs: dict[str, object] = {"user_id": async_admin_user.id}
        if _has_enriched:
            kwargs.update(
                action_type="send",
                message_text="content-must-not-leak",
                channel="telegram",
                reasoning="reasoning-must-not-leak",
                tasks="task-must-not-leak",
            )
        async with async_db() as db:
            log = HeartbeatLog(**kwargs)
            db.add(log)
            await db.commit()
            await db.refresh(log)
            log_id = log.id

        resp = await admin_async_client.get(
            f"/api/admin/users/{async_admin_user.id}/heartbeat-logs"
        )
        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert item["id"] == log_id
        assert item["user_id"] == async_admin_user.id
        assert "created_at" in item
        assert item["action_type"] == "send"
        if _has_enriched:
            assert item["channel"] == "telegram"
        for dropped in ("message_text", "reasoning", "tasks"):
            assert dropped not in item, f"{dropped!r} must not be in slim heartbeat response"
        if _has_enriched:
            for marker in (
                "content-must-not-leak",
                "reasoning-must-not-leak",
                "task-must-not-leak",
            ):
                assert marker not in resp.text, (
                    f"{marker!r} leaked into the slim heartbeat response"
                )

    async def test_get_heartbeat_logs_limit(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_admin_user: User,
    ) -> None:
        async with async_db() as db:
            for _ in range(5):
                db.add(HeartbeatLog(user_id=async_admin_user.id))
            await db.commit()

        resp = await admin_async_client.get(
            f"/api/admin/users/{async_admin_user.id}/heartbeat-logs?limit=2"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 5
        assert len(data["items"]) == 2

    async def test_get_heartbeat_logs_nonexistent_user(
        self,
        admin_async_client: httpx.AsyncClient,
    ) -> None:
        resp = await admin_async_client.get(
            "/api/admin/users/00000000-0000-0000-0000-000000000000/heartbeat-logs"
        )
        assert resp.status_code == 404


class TestLLMUsageLogs:
    async def test_empty_for_new_user(
        self,
        admin_async_client: httpx.AsyncClient,
        async_admin_user: User,
    ) -> None:
        resp = await admin_async_client.get(
            f"/api/admin/users/{async_admin_user.id}/llm-usage-logs"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data == {"total": 0, "items": []}

    async def test_returns_logs_most_recent_first(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_admin_user: User,
    ) -> None:
        async with async_db() as db:
            db.add(
                LLMUsageLog(
                    user_id=async_admin_user.id,
                    provider="openai",
                    model="gpt-5.4",
                    purpose="primary",
                    input_tokens=1000,
                    output_tokens=200,
                    total_tokens=1200,
                    cost=Decimal("0.0042"),
                )
            )
            db.add(
                LLMUsageLog(
                    user_id=async_admin_user.id,
                    provider="anthropic",
                    model="claude-opus-4-7",
                    purpose="compaction",
                    input_tokens=500,
                    output_tokens=50,
                    total_tokens=550,
                    cost=Decimal("0.0010"),
                    cache_read_input_tokens=400,
                )
            )
            await db.commit()

        resp = await admin_async_client.get(
            f"/api/admin/users/{async_admin_user.id}/llm-usage-logs"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert len(data["items"]) == 2
        assert data["items"][0]["provider"] == "anthropic"
        assert data["items"][0]["purpose"] == "compaction"
        assert data["items"][0]["cache_read_input_tokens"] == 400
        assert data["items"][1]["provider"] == "openai"
        assert isinstance(data["items"][0]["cost_usd"], str)

    async def test_limit_param_caps_results(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_admin_user: User,
    ) -> None:
        async with async_db() as db:
            for i in range(10):
                db.add(
                    LLMUsageLog(
                        user_id=async_admin_user.id,
                        provider="openai",
                        model="gpt-5.4",
                        purpose="primary",
                        input_tokens=i,
                        output_tokens=0,
                        total_tokens=i,
                        cost=Decimal("0"),
                    )
                )
            await db.commit()

        resp = await admin_async_client.get(
            f"/api/admin/users/{async_admin_user.id}/llm-usage-logs?limit=3"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 10
        assert len(data["items"]) == 3

    async def test_non_admin_blocked(
        self,
        non_admin_async_client: httpx.AsyncClient,
        async_test_user: User,
    ) -> None:
        resp = await non_admin_async_client.get(
            f"/api/admin/users/{async_test_user.id}/llm-usage-logs"
        )
        assert resp.status_code == 403

    async def test_nonexistent_user(
        self,
        admin_async_client: httpx.AsyncClient,
    ) -> None:
        resp = await admin_async_client.get(
            "/api/admin/users/00000000-0000-0000-0000-000000000000/llm-usage-logs"
        )
        assert resp.status_code == 404


class TestStagedMedia:
    """Diagnostic endpoint that surfaces the per-user ``staged_media`` table.

    Joint with the webhook-events endpoint, this is how a future
    "contractor lost photos" investigation skips the psql step.
    """

    async def test_empty_for_new_user(
        self,
        admin_async_client: httpx.AsyncClient,
        async_admin_user: User,
    ) -> None:
        resp = await admin_async_client.get(f"/api/admin/users/{async_admin_user.id}/staged-media")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["active"] == 0
        assert data["uploaded"] == 0
        # The cap default of 50 is documented in
        # ``backend.app.agent.media_staging.STAGING_MAX_PER_USER``; surface
        # the value so the admin UI can render "50 / 50" without round-
        # tripping a separate config call.
        assert data["cap"] >= 1
        assert data["items"] == []

    async def test_counts_and_receipt_breakdown(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_admin_user: User,
    ) -> None:
        """``uploaded`` separates receipts from raw bytes at a glance."""
        now = _dt.datetime.now(_dt.UTC)
        future = now + _dt.timedelta(days=7)
        async with async_db() as db:
            for i in range(3):
                db.add(
                    StagedMedia(
                        id=f"row-staged-{i}",
                        user_id=async_admin_user.id,
                        handle=f"media_handle_{i}",
                        original_url=f"bb_attach_{i}",
                        mime_type="image/jpeg",
                        disk_path=f"{async_admin_user.id}/media_handle_{i}.bin",
                        expires_at=future,
                        upload_service="companycam" if i < 2 else None,
                        upload_status="processed" if i < 2 else None,
                        uploaded_at=now if i < 2 else None,
                    )
                )
            await db.commit()

        resp = await admin_async_client.get(f"/api/admin/users/{async_admin_user.id}/staged-media")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        assert data["active"] == 3
        assert data["uploaded"] == 2
        assert len(data["items"]) == 3

    async def test_nonexistent_user(
        self,
        admin_async_client: httpx.AsyncClient,
    ) -> None:
        resp = await admin_async_client.get(
            "/api/admin/users/00000000-0000-0000-0000-000000000000/staged-media"
        )
        assert resp.status_code == 404


class TestWebhookEvents:
    """Diagnostic endpoint surfacing ``idempotency_keys`` for a user.

    The join with ``messages`` answers the key forensic question:
    "did this webhook arrive but vanish before a Message landed?"
    Used to spot approval-gate consumption or consumer-side failures
    without dropping to psql.
    """

    async def test_orphan_idempotency_row_only_with_include_orphans(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_admin_user: User,
    ) -> None:
        """Orphans hidden by default, opt in with ``include_orphans=true``.

        Idempotency rows have no ``user_id`` column, so an orphan
        cannot be attributed to a tenant. Returning orphans by
        default on a user-scoped endpoint would leak cross-tenant
        events to admins investigating an unrelated user.
        """
        async with async_db() as db:
            db.add(
                IdempotencyKey(external_id="bb_orphan-test-001"),
            )
            await db.commit()

        # Default: orphans hidden.
        resp = await admin_async_client.get(
            f"/api/admin/users/{async_admin_user.id}/webhook-events"
        )
        assert resp.status_code == 200
        ids_default = [it["external_id"] for it in resp.json()["items"]]
        assert "bb_orphan-test-001" not in ids_default

        # Opt in: orphan shows with ``message_persisted=False``.
        resp = await admin_async_client.get(
            f"/api/admin/users/{async_admin_user.id}/webhook-events?include_orphans=true"
        )
        assert resp.status_code == 200
        orphan = next(
            (it for it in resp.json()["items"] if it["external_id"] == "bb_orphan-test-001"),
            None,
        )
        assert orphan is not None
        assert orphan["message_persisted"] is False
        assert orphan["user_id"] is None
        assert orphan["media_count"] == 0

    async def test_persisted_event_joins_to_message(
        self,
        admin_async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_admin_user: User,
    ) -> None:
        async with async_db() as db:
            session = ChatSession(
                user_id=async_admin_user.id,
                session_id=f"{async_admin_user.id}_test_session",
            )
            db.add(session)
            await db.flush()
            db.add(IdempotencyKey(external_id="bb_persisted-test-001"))
            db.add(
                Message(
                    session_id=session.id,
                    seq=1,
                    direction="inbound",
                    body="hi",
                    external_message_id="bb_persisted-test-001",
                    media_urls_json=json.dumps(["att1", "att2", "att3"]),
                )
            )
            await db.commit()

        resp = await admin_async_client.get(
            f"/api/admin/users/{async_admin_user.id}/webhook-events"
        )
        assert resp.status_code == 200
        data = resp.json()
        hit = next(
            (it for it in data["items"] if it["external_id"] == "bb_persisted-test-001"),
            None,
        )
        assert hit is not None
        assert hit["message_persisted"] is True
        assert hit["user_id"] == async_admin_user.id
        assert hit["media_count"] == 3

    async def test_nonexistent_user(
        self,
        admin_async_client: httpx.AsyncClient,
    ) -> None:
        resp = await admin_async_client.get(
            "/api/admin/users/00000000-0000-0000-0000-000000000000/webhook-events"
        )
        assert resp.status_code == 404


class TestAdminStats:
    async def test_get_stats(self, admin_async_client: httpx.AsyncClient) -> None:
        resp = await admin_async_client.get("/api/admin/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_users" not in data
        assert "messages_this_month" not in data

    async def test_get_stats_returns_dashboard_fields(
        self,
        admin_async_client: httpx.AsyncClient,
    ) -> None:
        """Stats response must surface fields the Overview dashboard depends on."""
        resp = await admin_async_client.get("/api/admin/stats")
        assert resp.status_code == 200
        data = resp.json()
        for key in (
            "telegram_configured",
            "bluebubbles_configured",
            "twilio_configured",
        ):
            assert key in data, f"Missing dashboard field: {key}"


class TestAdminVersion:
    """Tests for GET /api/admin/version (overview card + auto-reload poll)."""

    async def test_returns_expected_shape(
        self,
        admin_async_client: httpx.AsyncClient,
    ) -> None:
        resp = await admin_async_client.get("/api/admin/version")
        assert resp.status_code == 200
        data = resp.json()
        for key in (
            "premium_version",
            "premium_commit",
            "oss_version",
            "oss_commit",
            "started_at",
        ):
            assert key in data, f"Missing version field: {key}"
            assert isinstance(data[key], str)
        # started_at must parse as ISO 8601 so the client can compare
        # values and trigger location.reload() on a deploy.
        _dt.datetime.fromisoformat(data["started_at"])

    async def test_non_admin_blocked(
        self,
        non_admin_async_client: httpx.AsyncClient,
    ) -> None:
        resp = await non_admin_async_client.get("/api/admin/version")
        assert resp.status_code == 403


class TestAdminChannelConfig:
    """Tests for GET/PUT /api/admin/channels/config."""

    async def test_get_channel_config(
        self,
        admin_async_client: httpx.AsyncClient,
    ) -> None:
        resp = await admin_async_client.get("/api/admin/channels/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "bluebubbles_server_url" in data
        assert "bluebubbles_password_set" in data
        assert "bluebubbles_configured" in data
        assert isinstance(data["bluebubbles_password_set"], bool)

    async def test_update_channel_config(
        self,
        admin_async_client: httpx.AsyncClient,
    ) -> None:
        resp = await admin_async_client.put(
            "/api/admin/channels/config",
            json={"bluebubbles_imessage_address": "admin@icloud.com"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["bluebubbles_imessage_address"] == "admin@icloud.com"

    async def test_non_admin_blocked(
        self,
        non_admin_async_client: httpx.AsyncClient,
    ) -> None:
        """Non-admin user gets 403 from admin channel config endpoints."""
        resp = await non_admin_async_client.get("/api/admin/channels/config")
        assert resp.status_code == 403

    async def test_update_empty_body_returns_400(
        self,
        admin_async_client: httpx.AsyncClient,
    ) -> None:
        resp = await admin_async_client.put("/api/admin/channels/config", json={})
        assert resp.status_code == 400

    async def test_update_rejects_comma_separated_chat_ids(
        self,
        admin_async_client: httpx.AsyncClient,
    ) -> None:
        resp = await admin_async_client.put(
            "/api/admin/channels/config",
            json={"telegram_allowed_chat_id": "123,456"},
        )
        assert resp.status_code == 422
        assert "comma" in resp.json()["detail"].lower()
