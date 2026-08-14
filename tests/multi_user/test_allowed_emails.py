"""Tests for allowed-email registration gating (issue #128).

Covers:
- Admin CRUD for allowed emails (list, add, delete)
- Registration blocked for unapproved emails in restricted mode
- Registration allowed for approved emails and admin email
- Open mode allows all registrations
- OAuth callback handles restricted mode
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import Session

from backend.app.auth.oauth_flow import RegistrationNotAllowed, get_or_create_user
from backend.app.middleware.rate_limit import _auth_rate_limiter
from backend.app.models import AllowedEmail, Subscription
from backend.app.routers.google_oauth import _create_state_token


@pytest.fixture(autouse=True)
def _clear_rate_limits() -> None:
    _auth_rate_limiter.reset()


class TestAllowedEmailAdmin:
    """Admin CRUD endpoints for allowed emails."""

    def test_list_empty(self, client: TestClient, test_subscription: Subscription) -> None:
        resp = client.get("/api/admin/allowed-emails")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    def test_add_allowed_email(self, client: TestClient, test_subscription: Subscription) -> None:
        resp = client.post(
            "/api/admin/allowed-emails",
            json={"email": "alice@example.com", "note": "Team member"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["email"] == "alice@example.com"
        assert data["note"] == "Team member"
        assert data["id"] > 0

    def test_add_normalizes_email(
        self, client: TestClient, test_subscription: Subscription
    ) -> None:
        resp = client.post(
            "/api/admin/allowed-emails",
            json={"email": "  BOB@Example.COM  "},
        )
        assert resp.status_code == 200
        assert resp.json()["email"] == "bob@example.com"

    def test_add_duplicate_returns_409(
        self, client: TestClient, test_subscription: Subscription
    ) -> None:
        client.post(
            "/api/admin/allowed-emails",
            json={"email": "dupe@example.com"},
        )
        resp = client.post(
            "/api/admin/allowed-emails",
            json={"email": "dupe@example.com"},
        )
        assert resp.status_code == 409

    def test_list_after_add(self, client: TestClient, test_subscription: Subscription) -> None:
        client.post(
            "/api/admin/allowed-emails",
            json={"email": "listed@example.com"},
        )
        resp = client.get("/api/admin/allowed-emails")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["email"] == "listed@example.com"

    def test_delete_allowed_email(
        self, client: TestClient, test_subscription: Subscription
    ) -> None:
        create_resp = client.post(
            "/api/admin/allowed-emails",
            json={"email": "todelete@example.com"},
        )
        email_id = create_resp.json()["id"]

        resp = client.delete(f"/api/admin/allowed-emails/{email_id}")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

        # Confirm deleted
        list_resp = client.get("/api/admin/allowed-emails")
        assert list_resp.json()["total"] == 0

    def test_delete_nonexistent_returns_404(
        self, client: TestClient, test_subscription: Subscription
    ) -> None:
        resp = client.delete("/api/admin/allowed-emails/99999")
        assert resp.status_code == 404


class TestRegistrationGating:
    """Registration restriction logic in get_or_create_user."""

    @pytest.mark.asyncio
    async def test_restricted_mode_blocks_unapproved(self, async_db: async_sessionmaker) -> None:
        """New user with unapproved email is rejected in restricted mode."""
        user_info = {
            "sub": "new_user_blocked",
            "email": "stranger@example.com",
            "name": "Stranger",
        }
        with (
            patch("backend.app.auth.oauth_flow.settings") as mock_settings,
        ):
            mock_settings.registration_mode = "restricted"
            mock_settings.admin_email = "admin@example.com"
            async with async_db() as db:
                with pytest.raises(RegistrationNotAllowed):
                    await get_or_create_user(db, user_info)

    @pytest.mark.asyncio
    async def test_restricted_mode_allows_approved(self, async_db: async_sessionmaker) -> None:
        """New user with approved email is allowed in restricted mode."""
        user_info = {
            "sub": "new_user_approved",
            "email": "approved@example.com",
            "name": "Approved User",
        }
        with patch("backend.app.auth.oauth_flow.settings") as mock_settings:
            mock_settings.registration_mode = "restricted"
            mock_settings.admin_email = "admin@example.com"
            async with async_db() as db:
                db.add(AllowedEmail(email="approved@example.com"))
                await db.commit()
                user = await get_or_create_user(db, user_info)
            assert user is not None

    @pytest.mark.asyncio
    async def test_restricted_mode_allows_admin_email(self, async_db: async_sessionmaker) -> None:
        """Admin email is always allowed even without being in allowed_emails."""
        user_info = {
            "sub": "admin_signup",
            "email": "admin@example.com",
            "name": "Admin",
        }
        with patch("backend.app.auth.oauth_flow.settings") as mock_settings:
            mock_settings.registration_mode = "restricted"
            mock_settings.admin_email = "admin@example.com"
            async with async_db() as db:
                user = await get_or_create_user(db, user_info)
            assert user is not None

    @pytest.mark.asyncio
    async def test_open_mode_allows_all(self, async_db: async_sessionmaker) -> None:
        """Open mode does not restrict registration."""
        user_info = {
            "sub": "open_user",
            "email": "anyone@example.com",
            "name": "Anyone",
        }
        with patch("backend.app.auth.oauth_flow.settings") as mock_settings:
            mock_settings.registration_mode = "open"
            mock_settings.admin_email = ""
            async with async_db() as db:
                user = await get_or_create_user(db, user_info)
            assert user is not None


class TestOAuthCallbackRestricted:
    """OAuth callback redirects with error for unapproved users."""

    def test_callback_restricted_user_gets_error_redirect(
        self, client: TestClient, db_session: Session
    ) -> None:
        state = _create_state_token()
        google_user = {"sub": "blocked_oauth", "name": "Blocked", "email": "blocked@example.com"}

        with (
            patch(
                "backend.app.routers.google_oauth.exchange_google_code",
                new_callable=AsyncMock,
                return_value=google_user,
            ),
            patch("backend.app.auth.oauth_flow.settings") as mock_settings,
        ):
            mock_settings.registration_mode = "restricted"
            mock_settings.admin_email = "admin@example.com"
            resp = client.get(
                "/api/auth/oauth/google/callback",
                params={"code": "valid_code", "state": state},
                cookies={"oauth_state": state},
                follow_redirects=False,
            )

        assert resp.status_code == 302
        location = resp.headers["location"]
        assert "/app/login#auth_error=" in location
        assert "not%20been%20approved" in location or "approved" in location
