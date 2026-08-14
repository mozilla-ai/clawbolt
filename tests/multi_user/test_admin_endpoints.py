"""Tests for expanded admin endpoints."""

import datetime
import uuid
from datetime import UTC

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.app.agent.file_store import get_user_store
from backend.app.models import Subscription, UsageQuota, User


class TestListUsersPagination:
    def test_returns_paginated_response(
        self, client: TestClient, test_user: User, test_subscription: Subscription
    ) -> None:
        """Should return total, offset, limit, and items."""
        resp = client.get("/api/admin/users")
        assert resp.status_code == 200
        data = resp.json()
        assert "total" in data
        assert "items" in data
        assert data["total"] >= 1
        assert data["offset"] == 0
        assert data["limit"] == 50

    def test_offset_and_limit(
        self, client: TestClient, test_user: User, test_subscription: Subscription
    ) -> None:
        """Custom offset/limit should be respected."""
        resp = client.get("/api/admin/users?offset=0&limit=1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["limit"] == 1
        assert len(data["items"]) <= 1

    def test_search_by_user_id(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """Search should filter by user_id."""
        resp = client.get("/api/admin/users?search=google_test")
        data = resp.json()
        assert data["total"] >= 1

    def test_search_no_results(
        self, client: TestClient, test_user: User, test_subscription: Subscription
    ) -> None:
        """Search with no matches should return empty items."""
        resp = client.get("/api/admin/users?search=zzz_nonexistent_zzz")
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    def test_includes_is_active(
        self, client: TestClient, test_user: User, test_subscription: Subscription
    ) -> None:
        """Response items should include is_active field."""
        resp = client.get("/api/admin/users")
        data = resp.json()
        item = data["items"][0]
        assert "is_active" in item


def _create_other_user(db_session: Session) -> User:
    """Create a non-admin user for tests that need a distinct target."""

    other = User(
        id=str(uuid.uuid4()),
        user_id=f"google_other_{uuid.uuid4().hex[:8]}",
        phone="",
        onboarding_complete=True,
    )
    db_session.add(other)
    db_session.commit()
    db_session.refresh(other)
    return other


class TestActivateDeactivate:
    @pytest.mark.asyncio
    async def test_deactivate_user(
        self,
        client: TestClient,
        db_session: Session,
        test_subscription: Subscription,
    ) -> None:
        """Should set is_active=False."""
        other = _create_other_user(db_session)
        resp = client.post(f"/api/admin/users/{other.id}/deactivate")
        assert resp.status_code == 200
        assert resp.json()["is_active"] is False
        updated = await get_user_store().get_by_id(other.id)
        assert updated is not None
        assert updated.is_active is False

    @pytest.mark.asyncio
    async def test_activate_user(
        self,
        client: TestClient,
        db_session: Session,
        test_subscription: Subscription,
    ) -> None:
        """Should set is_active=True."""
        other = _create_other_user(db_session)
        await get_user_store().update(other.id, is_active=False)
        resp = client.post(f"/api/admin/users/{other.id}/activate")
        assert resp.status_code == 200
        assert resp.json()["is_active"] is True
        updated = await get_user_store().get_by_id(other.id)
        assert updated is not None
        assert updated.is_active is True

    def test_activate_nonexistent_returns_404(
        self, client: TestClient, test_subscription: Subscription
    ) -> None:
        """Should return 404 for unknown user."""
        resp = client.post("/api/admin/users/99999/activate")
        assert resp.status_code == 404

    def test_deactivate_nonexistent_returns_404(
        self, client: TestClient, test_subscription: Subscription
    ) -> None:
        """Should return 404 for unknown user."""
        resp = client.post("/api/admin/users/99999/deactivate")
        assert resp.status_code == 404


class TestResetQuota:
    def test_resets_usage_to_zero(
        self,
        client: TestClient,
        db_session: Session,
        test_subscription: Subscription,
    ) -> None:
        """Should reset all usage counters to zero."""

        other = User(
            id=str(uuid.uuid4()),
            user_id=f"google_other_{uuid.uuid4().hex[:8]}",
            phone="",
            onboarding_complete=True,
        )
        db_session.add(other)
        db_session.commit()
        db_session.refresh(other)
        now = datetime.datetime.now(UTC)
        period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        quota = UsageQuota(
            user_id=other.id,
            period_start=period_start,
            messages_used=25,
            messages_limit=1000,
            tokens_used=50_000,
            tokens_limit=1_000_000,
        )
        db_session.add(quota)
        db_session.commit()

        resp = client.post(f"/api/admin/users/{other.id}/reset-quota")
        assert resp.status_code == 200
        data = resp.json()
        assert data["messages"]["used"] == 0
        assert data["tokens"]["used"] == 0

    def test_reset_nonexistent_returns_404(
        self, client: TestClient, test_subscription: Subscription
    ) -> None:
        """Should return 404 for unknown user."""
        resp = client.post("/api/admin/users/99999/reset-quota")
        assert resp.status_code == 404
