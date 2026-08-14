"""Endpoint tests for account router (issue #67).

Covers:
- GET /api/account/profile
- GET /api/account/usage
- GET /api/account/export
- DELETE /api/account/delete
"""

from __future__ import annotations

import datetime
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.auth.dependencies import get_current_user
from backend.app.middleware.rate_limit import _auth_rate_limiter
from backend.app.models import Subscription, UsageQuota, User


@pytest.fixture(autouse=True)
def _clear_rate_limits() -> None:
    _auth_rate_limiter.reset()


class TestProfile:
    def test_get_profile(
        self,
        client: TestClient,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        resp = client.get("/api/account/profile")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == test_user.id
        assert data["plan"] == "free"

    def test_profile_returns_role(
        self,
        client: TestClient,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """Profile endpoint must return the user's role for admin detection."""
        resp = client.get("/api/account/profile")
        assert resp.status_code == 200
        data = resp.json()
        assert data["role"] == "admin"  # conftest creates subscription with role="admin"

    def test_profile_without_subscription(
        self,
        client: TestClient,
        test_user: User,
    ) -> None:
        """User without subscription defaults to 'free' plan and 'user' role."""
        resp = client.get("/api/account/profile")
        assert resp.status_code == 200
        assert resp.json()["plan"] == "free"
        assert resp.json()["role"] == "user"


class TestAccountUsage:
    def test_get_usage(
        self,
        client: TestClient,
        test_user: User,
        test_quota: UsageQuota,
    ) -> None:
        resp = client.get("/api/account/usage")
        assert resp.status_code == 200
        data = resp.json()
        assert data["messages"]["used"] == 0
        assert data["messages"]["limit"] == 1000


class TestExport:
    def test_export_data(self, client: TestClient, test_user: User) -> None:
        with patch(
            "backend.app.routers.account.export_user_data",
            return_value={"user": {"id": test_user.id}},
        ):
            resp = client.get("/api/account/export")
        assert resp.status_code == 200
        data = resp.json()
        assert "user" in data


@pytest_asyncio.fixture
async def async_client(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> AsyncGenerator[httpx.AsyncClient]:
    """ASGI-driven async HTTP client wired to the per-test async DB.

    Mirrors the sync ``client`` fixture but uses ``httpx.AsyncClient`` +
    ``ASGITransport`` so async routes (``Depends(get_async_db)``) share
    the per-test connection bound by the ``async_db`` fixture.
    """
    from tests.multi_user.conftest import MULTI_USER_APP as app

    app.dependency_overrides[get_current_user] = lambda: async_test_user
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


class TestDeleteAccount:
    async def test_delete_account(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        # Set up subscription and quota through the async per-test
        # connection so the route (async DB) can see them.
        async with async_db() as db:
            db.add(
                Subscription(
                    user_id=async_test_user.id,
                    role="user",
                    plan="free",
                    status="active",
                )
            )
            now = datetime.datetime.now(datetime.UTC)
            period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            db.add(
                UsageQuota(
                    user_id=async_test_user.id,
                    period_start=period_start,
                    messages_used=0,
                    messages_limit=1000,
                    tokens_used=0,
                    tokens_limit=1_000_000,
                )
            )
            await db.commit()

        resp = await async_client.delete("/api/account/delete")
        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"

        # Verify user is deactivated by reading through the same async
        # connection (the route writes the deactivation through
        # ``store.update_async`` -> ``db_session_async()`` -> rebound
        # async factory -> per-test connection).
        async with async_db() as db:
            user = (
                await db.execute(select(User).where(User.id == async_test_user.id))
            ).scalar_one_or_none()
        assert user is not None
        assert user.is_active is False
