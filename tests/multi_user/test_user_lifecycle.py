"""User lifecycle integration test (issue #71).

Tests the full user journey:
1. Signup via OAuth
2. View profile (verify in DB)
3. Delete account

After issue #395, ``delete_account`` runs on an ``AsyncSession`` while
the sync ``client`` fixture seeds the user through a separate connection.
The deep ``delete_account`` semantics are covered in
``tests/test_account_deletion.py`` against the async fixture; here we
verify the full HTTP-layer flow (signup -> profile -> delete-route)
with the deletion service patched out so the test does not need to
bridge the cross-API transaction split documented in ``conftest.py``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.app.agent.file_store import get_user_store
from backend.app.middleware.rate_limit import _auth_rate_limiter
from backend.app.models import Subscription, UsageQuota
from backend.app.routers.google_oauth import _create_state_token


@pytest.fixture(autouse=True)
def _clear_rate_limits() -> None:
    _auth_rate_limiter.reset()


class TestUserLifecycle:
    def test_signup_profile_delete(
        self,
        client: TestClient,
        db_session: Session,
    ) -> None:
        """Full lifecycle: signup -> profile -> delete."""
        state = _create_state_token()
        google_info = {
            "sub": "lifecycle_user",
            "name": "Lifecycle User",
            "email": "lifecycle@example.com",
        }

        with patch(
            "backend.app.routers.google_oauth.exchange_google_code",
            new_callable=AsyncMock,
            return_value=google_info,
        ):
            signup_resp = client.post(
                "/api/auth/oauth/google/exchange",
                json={"code": "lifecycle_code", "state": state},
            )

        assert signup_resp.status_code == 200
        tokens = signup_resp.json()
        user_id = tokens["user_id"]
        assert tokens["access_token"]

        store = get_user_store()
        user_data = asyncio.run(store.get_by_id(user_id))
        assert user_data is not None
        assert user_data.user_id == "google_lifecycle_user"

        sub = db_session.query(Subscription).filter(Subscription.user_id == user_id).first()
        assert sub is not None
        assert sub.plan == "free"

        from backend.app.billing.plans import PLANS

        quota = db_session.query(UsageQuota).filter(UsageQuota.user_id == user_id).first()
        assert quota is not None
        assert quota.messages_limit == PLANS["free"].messages_per_month

        # Patch out the deletion service: the user was seeded through the
        # sync per-test connection (sync OAuth route), but the delete
        # endpoint now resolves an ``AsyncSession`` on a different
        # connection. Driving the real service here would either miss
        # the user or write outside the per-test transaction. The deep
        # behavior is covered in tests/test_account_deletion.py.
        with patch(
            "backend.app.routers.account._delete_account",
            new_callable=AsyncMock,
        ) as mock_delete:
            delete_resp = client.delete("/api/account/delete")

        assert delete_resp.status_code == 200
        assert delete_resp.json()["status"] == "deleted"
        mock_delete.assert_called_once()
