"""Tests for the GET /auth/oauth/google/callback endpoint.

This endpoint was missing entirely (issue: OAuth 404), so these tests
guard against its accidental removal or breakage.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import Session

from backend.app.middleware.rate_limit import _auth_rate_limiter
from backend.app.models import Subscription, UsageQuota
from backend.app.models import User as OssUser
from backend.app.routers.google_oauth import _create_state_token


@pytest.fixture(autouse=True)
def _clear_rate_limits() -> None:
    _auth_rate_limiter.reset()


class TestOAuthCallback:
    """Tests for GET /auth/oauth/google/callback."""

    def _google_user(self, sub: str = "callback_test") -> dict[str, str]:
        return {"sub": sub, "name": "Test User", "email": "test@example.com"}

    def test_callback_redirects_with_refresh_token(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Successful callback redirects to /app#refresh_token=<token>."""
        state = _create_state_token()

        with patch(
            "backend.app.routers.google_oauth.exchange_google_code",
            new_callable=AsyncMock,
            return_value=self._google_user(),
        ):
            resp = client.get(
                "/api/auth/oauth/google/callback",
                params={"code": "valid_code", "state": state},
                cookies={"oauth_state": state},
                follow_redirects=False,
            )

        assert resp.status_code == 302
        location = resp.headers["location"]
        assert location.startswith("/app#refresh_token=")

    def test_callback_error_redirects_to_login(self, client: TestClient) -> None:
        """Google error redirects to /app/login#auth_error=..."""
        resp = client.get(
            "/api/auth/oauth/google/callback",
            params={"error": "access_denied"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "/app/login#auth_error=" in resp.headers["location"]

    def test_callback_missing_code_redirects_to_login(self, client: TestClient) -> None:
        """Missing authorization code redirects with error."""
        resp = client.get(
            "/api/auth/oauth/google/callback",
            params={"state": "some_state"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "/app/login#auth_error=" in resp.headers["location"]

    def test_callback_invalid_state_redirects_to_login(self, client: TestClient) -> None:
        """Mismatched CSRF state redirects with error."""
        resp = client.get(
            "/api/auth/oauth/google/callback",
            params={"code": "valid_code", "state": "wrong_state"},
            cookies={"oauth_state": "different_state"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "/app/login#auth_error=" in resp.headers["location"]

    def test_google_redirect_exists(self, client: TestClient) -> None:
        """GET /auth/oauth/google redirects to Google (not 404/405)."""
        resp = client.get(
            "/api/auth/oauth/google",
            follow_redirects=False,
        )
        # Should redirect to Google's OAuth URL (or 500 if client_id is empty)
        assert resp.status_code in (302, 500)


class TestDeactivatedAccount:
    """Deactivated users (User.is_active=False) cannot sign in via OAuth."""

    @pytest.mark.asyncio
    async def test_get_or_create_user_raises_for_deactivated(
        self,
        async_db: async_sessionmaker,
    ) -> None:
        from backend.app.agent.file_store import get_user_store
        from backend.app.auth.oauth_flow import AccountDeactivated, get_or_create_user

        store = get_user_store()
        async with async_db() as db:
            user = await store.create(user_id="google_deactivated_test")
            row = (
                await db.execute(select(OssUser).where(OssUser.id == user.id))
            ).scalar_one_or_none()
            assert row is not None
            row.is_active = False
            await db.commit()

            google_info = {
                "sub": "deactivated_test",
                "name": "Test User",
                "email": "deactivated@example.com",
            }

            with pytest.raises(AccountDeactivated):
                await get_or_create_user(db, google_info)

    def test_callback_redirects_with_deactivated_message(
        self, client: TestClient, db_session: Session
    ) -> None:
        from backend.app.agent.file_store import get_user_store
        from backend.app.models import User as OssUser
        from backend.app.routers.google_oauth import _DEACTIVATED_LOGIN_MESSAGE

        store = get_user_store()
        user = asyncio.run(store.create(user_id="google_deactivated_callback"))
        row = db_session.query(OssUser).filter_by(id=user.id).first()
        assert row is not None
        row.is_active = False
        db_session.commit()

        state = _create_state_token()
        with patch(
            "backend.app.routers.google_oauth.exchange_google_code",
            new_callable=AsyncMock,
            return_value={
                "sub": "deactivated_callback",
                "name": "Test User",
                "email": "deactivated@example.com",
            },
        ):
            resp = client.get(
                "/api/auth/oauth/google/callback",
                params={"code": "valid_code", "state": state},
                cookies={"oauth_state": state},
                follow_redirects=False,
            )

        assert resp.status_code == 302
        location = resp.headers["location"]
        assert location.startswith("/app/login#auth_error=")
        assert quote(_DEACTIVATED_LOGIN_MESSAGE, safe="") in location


class TestEmailBackfill:
    """Returning users with missing subscription email get it backfilled (#154)."""

    @pytest.mark.asyncio
    async def test_existing_user_gets_email_backfilled(
        self,
        async_db: async_sessionmaker,
    ) -> None:
        """When a user logs in and their subscription email is empty, backfill it."""
        from backend.app.agent.file_store import get_user_store
        from backend.app.auth.oauth_flow import get_or_create_user

        store = get_user_store()
        async with async_db() as db:
            user = await store.create(user_id="google_backfill_test")
            db.add(Subscription(user_id=user.id, plan="free", status="active", email=""))
            await db.commit()

            google_info = {
                "sub": "backfill_test",
                "name": "Test User",
                "email": "backfill@example.com",
            }

            await get_or_create_user(db, google_info)
            updated = (
                await db.execute(select(Subscription).where(Subscription.user_id == user.id))
            ).scalar_one_or_none()
        assert updated is not None
        assert updated.email == "backfill@example.com"

    @pytest.mark.asyncio
    async def test_existing_email_not_overwritten(
        self,
        async_db: async_sessionmaker,
    ) -> None:
        """When subscription already has an email, do not overwrite it."""
        from backend.app.agent.file_store import get_user_store
        from backend.app.auth.oauth_flow import get_or_create_user

        store = get_user_store()
        async with async_db() as db:
            user = await store.create(user_id="google_keep_email_test")
            db.add(
                Subscription(
                    user_id=user.id,
                    plan="free",
                    status="active",
                    email="original@example.com",
                )
            )
            await db.commit()

            google_info = {
                "sub": "keep_email_test",
                "name": "Test User",
                "email": "different@example.com",
            }

            await get_or_create_user(db, google_info)
            updated = (
                await db.execute(select(Subscription).where(Subscription.user_id == user.id))
            ).scalar_one_or_none()
        assert updated is not None
        assert updated.email == "original@example.com"


class TestAsyncProvisioningRecovery:
    """AsyncSession signup path must heal partial state and stay atomic."""

    @pytest.mark.asyncio
    async def test_existing_user_missing_subscription_and_quota_are_healed(
        self,
        async_db: async_sessionmaker,
    ) -> None:
        from backend.app.auth.oauth_flow import get_or_create_user

        async with async_db() as db:
            user = OssUser(user_id="google_heal_missing_rows")
            db.add(user)
            await db.commit()
            await db.refresh(user)

            dto = await get_or_create_user(
                db,
                {
                    "sub": "heal_missing_rows",
                    "name": "Test User",
                    "email": "heal@example.com",
                },
            )

            subscription = (
                await db.execute(select(Subscription).where(Subscription.user_id == user.id))
            ).scalar_one_or_none()
            quotas = (
                (await db.execute(select(UsageQuota).where(UsageQuota.user_id == user.id)))
                .scalars()
                .all()
            )

        assert dto.id == user.id
        assert subscription is not None
        assert subscription.email == "heal@example.com"
        assert subscription.plan == "free"
        assert len(quotas) == 1

    @pytest.mark.asyncio
    async def test_async_signup_rolls_back_when_quota_bootstrap_fails(
        self,
        async_db: async_sessionmaker,
    ) -> None:
        from pathlib import Path

        from backend.app.auth.oauth_flow import get_or_create_user
        from backend.app.config import settings as oss_settings

        google_info = {
            "sub": "atomic_failure_test",
            "name": "Test User",
            "email": "atomic@example.com",
        }
        data_root = Path(oss_settings.data_dir)
        dirs_before = {path.name for path in data_root.iterdir()} if data_root.exists() else set()

        async with async_db() as db:
            with (
                patch(
                    "backend.app.auth.oauth_flow.get_current_quota",
                    new_callable=AsyncMock,
                    side_effect=RuntimeError("quota bootstrap failed"),
                ),
                pytest.raises(RuntimeError, match="quota bootstrap failed"),
            ):
                await get_or_create_user(db, google_info)

            await db.rollback()

            user = (
                await db.execute(
                    select(OssUser).where(OssUser.user_id == "google_atomic_failure_test")
                )
            ).scalar_one_or_none()
            subscription = (
                await db.execute(
                    select(Subscription).where(Subscription.email == "atomic@example.com")
                )
            ).scalar_one_or_none()

        assert user is None
        assert subscription is None
        dirs_after = {path.name for path in data_root.iterdir()} if data_root.exists() else set()
        assert dirs_after == dirs_before


class TestReloginReprovisioning:
    """OAuth re-login must re-provision the user so BOOTSTRAP.md is restored."""

    def test_relogin_reprovisions_missing_bootstrap(
        self,
        client: TestClient,
        db_session: Session,
    ) -> None:
        """Returning user whose BOOTSTRAP.md was wiped gets it regenerated.

        Regression: a user who was soft-deleted (or whose on-disk data was
        lost) and who had onboarding_complete=False should land back in
        onboarding on re-login. Previously get_or_create_user returned the
        existing user without re-running provision_user after the outer
        commit, so BOOTSTRAP.md was never re-created and onboarding
        silently skipped.
        """
        from pathlib import Path

        from backend.app.agent.file_store import get_user_store
        from backend.app.config import settings as oss_settings
        from backend.app.models import User as OssUser

        store = get_user_store()
        user = asyncio.run(store.create(user_id="google_relogin_test"))
        # Simulate prior soft-delete: clear text columns, reset onboarding,
        # leave user row intact, wipe filesystem.
        asyncio.run(
            store.update(
                user.id,
                soul_text="",
                user_text="",
                heartbeat_text="",
                onboarding_complete=False,
            )
        )
        user_dir = Path(oss_settings.data_dir) / str(user.id)
        bootstrap = user_dir / "BOOTSTRAP.md"
        if bootstrap.exists():
            bootstrap.unlink()
        assert not bootstrap.exists()

        db_session.add(Subscription(user_id=user.id, plan="free", status="active", email=""))
        db_session.commit()

        state = _create_state_token()
        with patch(
            "backend.app.routers.google_oauth.exchange_google_code",
            new_callable=AsyncMock,
            return_value={
                "sub": "relogin_test",
                "name": "Test User",
                "email": "relogin@example.com",
            },
        ):
            resp = client.get(
                "/api/auth/oauth/google/callback",
                params={"code": "valid_code", "state": state},
                cookies={"oauth_state": state},
                follow_redirects=False,
            )

        assert resp.status_code == 302
        reseeded = db_session.query(OssUser).filter_by(id=user.id).first()
        assert bootstrap.exists()
        assert reseeded is not None
        assert reseeded.soul_text
        assert reseeded.user_text
