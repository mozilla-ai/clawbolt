"""Endpoint tests for OAuth signup flow (issue #65).

Covers:
- GET /api/auth/oauth/google/state (state token generation)
- POST /api/auth/oauth/google/exchange (code exchange, signup, login)
- Error handling (invalid state, invalid code, missing sub)
- Admin auto-promotion via admin_email setting
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.app.middleware.rate_limit import _auth_rate_limiter
from backend.app.models import Subscription, UsageQuota
from backend.app.routers.google_oauth import _create_state_token


@pytest.fixture(autouse=True)
def _clear_rate_limits() -> None:
    """Reset the auth rate limiter before each test."""
    _auth_rate_limiter.reset()


class TestGetOAuthState:
    def test_returns_state_token(self, client: TestClient) -> None:
        resp = client.get("/api/auth/oauth/google/state")
        assert resp.status_code == 200
        data = resp.json()
        assert "state" in data
        assert len(data["state"]) > 0

    def test_state_token_is_valid_jwt(self, client: TestClient) -> None:
        """State token is a decodable JWT with the expected claims."""
        import jwt as pyjwt

        from backend.app.routers.google_oauth import _get_state_signing_key

        resp = client.get("/api/auth/oauth/google/state")
        state = resp.json()["state"]

        from backend.app.config import settings

        payload = pyjwt.decode(
            state,
            _get_state_signing_key(),
            algorithms=[settings.jwt_algorithm],
        )
        assert payload["type"] == "oauth_state"
        assert "iat" in payload
        assert "exp" in payload


class TestGoogleOAuth:
    """Tests for POST /api/auth/oauth/google/exchange."""

    def _google_user(
        self,
        sub: str = "oauth_test_sub",
        name: str = "Test OAuth User",
        email: str = "test@example.com",
    ) -> dict[str, str]:
        return {"sub": sub, "name": name, "email": email}

    def test_new_user_signup(self, client: TestClient, db_session: Session) -> None:
        """Full signup flow: valid state + valid code creates user, subscription, quota."""
        state = _create_state_token()
        google_info = self._google_user()

        with patch(
            "backend.app.routers.google_oauth.exchange_google_code",
            new_callable=AsyncMock,
            return_value=google_info,
        ):
            resp = client.post(
                "/api/auth/oauth/google/exchange",
                json={"code": "valid_code", "state": state},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["token_type"] == "bearer"
        assert isinstance(data["user_id"], str)
        assert len(data["user_id"]) > 0

        user_id = data["user_id"]

        # Verify user was persisted to the database
        from backend.app.agent.file_store import get_user_store

        store = get_user_store()
        import asyncio

        user_data = asyncio.run(store.get_by_id(user_id))
        assert user_data is not None
        assert user_data.user_id == "google_oauth_test_sub"

        # Verify subscription was created
        sub = db_session.query(Subscription).filter(Subscription.user_id == user_id).first()
        assert sub is not None
        assert sub.plan == "free"
        assert sub.status == "active"

        # Verify quota was created
        from backend.app.billing.plans import PLANS

        quota = db_session.query(UsageQuota).filter(UsageQuota.user_id == user_id).first()
        assert quota is not None
        assert quota.messages_used == 0
        assert quota.messages_limit == PLANS["free"].messages_per_month

    def test_existing_user_login(self, client: TestClient, db_session: Session) -> None:
        """Existing user logs in, same user_id returned."""
        state1 = _create_state_token()
        state2 = _create_state_token()
        google_info = self._google_user(sub="returning_user")

        with patch(
            "backend.app.routers.google_oauth.exchange_google_code",
            new_callable=AsyncMock,
            return_value=google_info,
        ):
            resp1 = client.post(
                "/api/auth/oauth/google/exchange",
                json={"code": "code1", "state": state1},
            )
            resp2 = client.post(
                "/api/auth/oauth/google/exchange",
                json={"code": "code2", "state": state2},
            )

        assert resp1.status_code == 200
        assert resp2.status_code == 200
        assert resp1.json()["user_id"] == resp2.json()["user_id"]

    def test_admin_auto_promotion(self, client: TestClient, db_session: Session) -> None:
        """User whose email matches admin_email gets role='admin' on Subscription."""
        state = _create_state_token()
        google_info = self._google_user(sub="admin_sub", email="boss@example.com")

        with (
            patch(
                "backend.app.routers.google_oauth.exchange_google_code",
                new_callable=AsyncMock,
                return_value=google_info,
            ),
            patch(
                "backend.app.auth.oauth_flow.settings.admin_email",
                "boss@example.com",
            ),
        ):
            resp = client.post(
                "/api/auth/oauth/google/exchange",
                json={"code": "admin_code", "state": state},
            )

        assert resp.status_code == 200
        user_id = resp.json()["user_id"]

        # Verify role via Subscription table
        sub = db_session.query(Subscription).filter(Subscription.user_id == user_id).first()
        assert sub is not None
        assert sub.role == "admin"

    def test_invalid_state_rejected(self, client: TestClient) -> None:
        """Invalid state token returns 400."""
        resp = client.post(
            "/api/auth/oauth/google/exchange",
            json={"code": "some_code", "state": "invalid.state.token"},
        )
        assert resp.status_code == 400
        assert "invalid oauth state" in resp.json()["detail"].lower()

    def test_expired_state_rejected(self, client: TestClient) -> None:
        """Expired state token returns 400."""
        import datetime

        import jwt

        from backend.app.routers.google_oauth import _get_state_signing_key

        now = datetime.datetime.now(datetime.UTC)
        expired_payload = {
            "type": "oauth_state",
            "iat": now - datetime.timedelta(minutes=10),
            "exp": now - datetime.timedelta(minutes=5),
        }
        from backend.app.config import settings

        expired_state = jwt.encode(
            expired_payload,
            _get_state_signing_key(),
            algorithm=settings.jwt_algorithm,
        )

        resp = client.post(
            "/api/auth/oauth/google/exchange",
            json={"code": "some_code", "state": expired_state},
        )
        assert resp.status_code == 400
        assert "expired" in resp.json()["detail"].lower()

    def test_google_code_exchange_failure(self, client: TestClient) -> None:
        """Google API failure returns 400."""
        state = _create_state_token()

        with patch(
            "backend.app.routers.google_oauth.exchange_google_code",
            new_callable=AsyncMock,
            side_effect=Exception("Google API error"),
        ):
            resp = client.post(
                "/api/auth/oauth/google/exchange",
                json={"code": "bad_code", "state": state},
            )

        assert resp.status_code == 400
        assert "invalid authorization code" in resp.json()["detail"].lower()

    def test_missing_sub_in_google_response(self, client: TestClient) -> None:
        """Google response without 'sub' returns 400."""
        state = _create_state_token()

        with patch(
            "backend.app.routers.google_oauth.exchange_google_code",
            new_callable=AsyncMock,
            return_value={"name": "No Sub User", "email": "nosub@example.com"},
        ):
            resp = client.post(
                "/api/auth/oauth/google/exchange",
                json={"code": "no_sub_code", "state": state},
            )

        assert resp.status_code == 400
        assert "invalid google user info" in resp.json()["detail"].lower()

    def test_access_token_rejects_as_state(self, client: TestClient) -> None:
        """An access token cannot be used as a state token."""
        from backend.app.auth.jwt_auth import create_access_token

        access_token = create_access_token("test-user-id")
        resp = client.post(
            "/api/auth/oauth/google/exchange",
            json={"code": "some_code", "state": access_token},
        )
        assert resp.status_code == 400
