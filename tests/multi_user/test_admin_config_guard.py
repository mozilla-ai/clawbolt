"""Tests for AdminConfigGuardMiddleware.

Verifies that non-admin users cannot PUT to model/channels config
endpoints, and cannot GET the model config (LLM provider/model is treated
as an admin-only operational detail in multi-tenant deployments).

The middleware reads the Subscription row through ``db_session_async()``
after the #398 conversion. Stitching that to the per-test sync transaction
(``_isolate_stores``) is non-trivial: the asyncpg-backed engine and
psycopg2-backed engine do not share connections, and writing through
``async_db()`` puts the Subscription on a different transaction than the
sync route handlers (which still write ``app_settings.updated_by_user_id``
via the sync session). To keep the test surface focused on the guard
itself, the request-stack tests patch ``_is_admin`` with a sync helper
that reads the existing per-test sync session; the production async DB
read in ``_is_admin`` is covered separately by ``TestIsAdminAsyncDB``,
which drives ``async_db`` directly.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import Session

from backend.app.auth.jwt_auth import create_access_token, decode_access_token
from backend.app.database import get_async_db
from backend.app.middleware.admin_config_guard import _is_admin
from backend.app.middleware.rate_limit import _auth_rate_limiter
from backend.app.models import Subscription, User
from tests.multi_user.conftest import _SyncToAsyncSessionProxy, open_test_db_session


@pytest.fixture(autouse=True)
def _clear_rate_limits() -> None:
    _auth_rate_limiter.reset()


def _sync_is_admin(token: str) -> bool:
    """Sync stand-in for ``_is_admin`` that reads the per-test sync session.

    Mirrors the production logic (decode JWT, look up Subscription.role) but
    on the sync ``SessionLocal()`` so it sees rows committed inside the
    per-test ``_isolate_stores`` transaction.
    """
    try:
        payload = decode_access_token(token)
    except Exception:
        return False
    user_id = payload.get("sub")
    if not user_id:
        return False
    db = open_test_db_session()
    try:
        sub = db.execute(
            select(Subscription).where(Subscription.user_id == user_id)
        ).scalar_one_or_none()
        return bool(sub and sub.role == "admin")
    finally:
        db.close()


async def _async_sync_is_admin(token: str) -> bool:
    """Async wrapper around ``_sync_is_admin`` so the middleware's ``await`` works."""
    return _sync_is_admin(token)


@pytest.fixture()
def jwt_client(db_session: Session, test_user: User) -> Generator[TestClient]:
    """Test client that does NOT override get_current_user (uses real JWT auth).

    After #429 ``resolve_multi_user`` looks the User up via
    ``db_session_async()`` directly (not via ``Depends(get_async_db)``),
    so a dependency override is not enough. Patching
    ``backend.app.main.db_session_async`` with an async context
    manager that yields a ``_SyncToAsyncSessionProxy`` over
    ``db_session`` keeps the JWT user lookup on the same per-test
    sync connection as test setup. ``get_async_db`` is also overridden
    so any downstream admin dep stays on the same connection.
    """
    from contextlib import asynccontextmanager

    from tests.multi_user.conftest import MULTI_USER_APP as app

    async def _yield_async_proxy() -> AsyncGenerator[_SyncToAsyncSessionProxy]:
        yield _SyncToAsyncSessionProxy(db_session)

    @asynccontextmanager
    async def _patched_db_session_async() -> AsyncGenerator[_SyncToAsyncSessionProxy]:
        yield _SyncToAsyncSessionProxy(db_session)

    app.dependency_overrides[get_async_db] = _yield_async_proxy
    mock_manager = MagicMock()
    mock_manager.start_all = AsyncMock(return_value=[])
    mock_manager.stop_all = AsyncMock()
    _settings_store_mock = MagicMock()
    _settings_store_mock.load = AsyncMock(return_value={})
    _settings_store_mock.save = AsyncMock()
    _settings_store_mock.delete = AsyncMock()
    with (
        patch(
            "backend.app.middleware.admin_config_guard._is_admin",
            new=_async_sync_is_admin,
        ),
        patch("backend.app.main.db_session_async", new=_patched_db_session_async),
        patch("backend.app.main.get_settings_store", return_value=_settings_store_mock),
        patch("backend.app.main.import_legacy_config_json", new_callable=AsyncMock),
        patch("backend.app.main.apply_to_settings", return_value={}),
        patch("backend.app.main.load_dotenv"),
        patch("backend.app.main._verify_llm_settings", new_callable=AsyncMock),
        patch("backend.app.main.heartbeat_scheduler"),
        patch("backend.app.main.oauth_refresh_scheduler"),
        patch("backend.app.main.get_manager", return_value=mock_manager),
        TestClient(app) as c,
    ):
        yield c
    from backend.app.auth.dependencies import get_current_user

    app.dependency_overrides.pop(get_async_db, None)
    app.dependency_overrides.pop(get_current_user, None)


class TestAdminConfigGuard:
    def test_non_admin_blocked_from_model_config(
        self,
        jwt_client: TestClient,
        test_user: User,
        db_session: Session,
    ) -> None:
        """Non-admin user gets 403 when trying to PUT model config."""
        sub = Subscription(
            user_id=test_user.id,
            role="user",
            plan="free",
            status="active",
        )
        db_session.add(sub)
        db_session.commit()

        token = create_access_token(test_user.id)
        resp = jwt_client.put(
            "/api/user/model/config",
            json={"llm_provider": "openai", "llm_model": "gpt-4o"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403
        assert "Admin access required" in resp.json()["detail"]

    def test_admin_allowed_model_config(
        self,
        jwt_client: TestClient,
        test_user: User,
        db_session: Session,
    ) -> None:
        """Admin user can PUT model config."""
        sub = Subscription(
            user_id=test_user.id,
            role="admin",
            plan="free",
            status="active",
        )
        db_session.add(sub)
        db_session.commit()

        token = create_access_token(test_user.id)
        resp = jwt_client.put(
            "/api/user/model/config",
            json={"llm_provider": "openai", "llm_model": "gpt-4o"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200

    def test_non_admin_blocked_from_channel_config(
        self,
        jwt_client: TestClient,
        test_user: User,
        db_session: Session,
    ) -> None:
        """Non-admin user gets 403 when trying to PUT channel config."""
        sub = Subscription(
            user_id=test_user.id,
            role="user",
            plan="free",
            status="active",
        )
        db_session.add(sub)
        db_session.commit()

        token = create_access_token(test_user.id)
        resp = jwt_client.put(
            "/api/user/channels/config",
            json={"bluebubbles_server_url": "https://evil.example.com"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403
        assert "Admin access required" in resp.json()["detail"]

    def test_admin_allowed_channel_config(
        self,
        jwt_client: TestClient,
        test_user: User,
        db_session: Session,
    ) -> None:
        """Admin user can PUT channel config."""
        sub = Subscription(
            user_id=test_user.id,
            role="admin",
            plan="free",
            status="active",
        )
        db_session.add(sub)
        db_session.commit()

        token = create_access_token(test_user.id)
        resp = jwt_client.put(
            "/api/user/channels/config",
            json={"bluebubbles_imessage_address": "admin@icloud.com"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200

    def test_non_admin_can_get_channel_config(
        self,
        jwt_client: TestClient,
        test_user: User,
        db_session: Session,
    ) -> None:
        """Non-admin user can still GET (read) channel config."""
        sub = Subscription(
            user_id=test_user.id,
            role="user",
            plan="free",
            status="active",
        )
        db_session.add(sub)
        db_session.commit()

        token = create_access_token(test_user.id)
        resp = jwt_client.get(
            "/api/user/channels/config",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200

    def test_non_admin_blocked_from_get_model_config(
        self,
        jwt_client: TestClient,
        test_user: User,
        db_session: Session,
    ) -> None:
        """Non-admin user gets 403 when trying to GET model config.

        In multi-tenant deployments the LLM provider/model is an admin-only
        operational detail. Tenants should not see what model the platform
        runs on.
        """
        sub = Subscription(
            user_id=test_user.id,
            role="user",
            plan="free",
            status="active",
        )
        db_session.add(sub)
        db_session.commit()

        token = create_access_token(test_user.id)
        resp = jwt_client.get(
            "/api/user/model/config",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403
        assert "Admin access required" in resp.json()["detail"]

    def test_admin_can_get_model_config(
        self,
        jwt_client: TestClient,
        test_user: User,
        db_session: Session,
    ) -> None:
        """Admin user can GET model config."""
        sub = Subscription(
            user_id=test_user.id,
            role="admin",
            plan="free",
            status="active",
        )
        db_session.add(sub)
        db_session.commit()

        token = create_access_token(test_user.id)
        resp = jwt_client.get(
            "/api/user/model/config",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200

    def test_non_admin_blocked_from_get_system_prompt(
        self,
        jwt_client: TestClient,
        test_user: User,
        db_session: Session,
    ) -> None:
        """Non-admin user gets 403 when trying to GET the conversation system prompt.

        The reconstructed system prompt reveals the operator's preamble,
        active tool wiring, and connected integrations. In multi-tenant
        deployments tenants should not see that operational detail.
        """
        sub = Subscription(
            user_id=test_user.id,
            role="user",
            plan="free",
            status="active",
        )
        db_session.add(sub)
        db_session.commit()

        token = create_access_token(test_user.id)
        resp = jwt_client.get(
            "/api/user/conversation/system-prompt",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403
        assert "Admin access required" in resp.json()["detail"]

    def test_admin_not_blocked_from_get_system_prompt(
        self,
        jwt_client: TestClient,
        test_user: User,
        db_session: Session,
    ) -> None:
        """Admin user passes the guard for the conversation system prompt.

        With no ChatSession seeded the OSS handler returns 404
        ("No conversation yet"). The handler being reached at all
        (a non-403 response) proves the guard let the request through.
        """
        sub = Subscription(
            user_id=test_user.id,
            role="admin",
            plan="free",
            status="active",
        )
        db_session.add(sub)
        db_session.commit()

        token = create_access_token(test_user.id)
        resp = jwt_client.get(
            "/api/user/conversation/system-prompt",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code != 403
        assert resp.status_code == 404


class TestIsAdminAsyncDB:
    """Direct coverage of the async ``_is_admin`` helper, including the
    ``db_session_async()`` lookup path that the request-stack tests above
    cannot exercise without cross-API transaction stitching.
    """

    @pytest.mark.asyncio
    async def test_admin_role_returns_true(
        self,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        async with async_db() as db:
            sub = Subscription(
                user_id=async_test_user.id,
                role="admin",
                plan="free",
                status="active",
            )
            db.add(sub)
            await db.commit()

        token = create_access_token(async_test_user.id)
        assert await _is_admin(token) is True

    @pytest.mark.asyncio
    async def test_user_role_returns_false(
        self,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        async with async_db() as db:
            sub = Subscription(
                user_id=async_test_user.id,
                role="user",
                plan="free",
                status="active",
            )
            db.add(sub)
            await db.commit()

        token = create_access_token(async_test_user.id)
        assert await _is_admin(token) is False

    @pytest.mark.asyncio
    async def test_no_subscription_returns_false(
        self,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        token = create_access_token(async_test_user.id)
        assert await _is_admin(token) is False

    @pytest.mark.asyncio
    async def test_invalid_token_returns_false(self) -> None:
        assert await _is_admin("not-a-real-jwt") is False
