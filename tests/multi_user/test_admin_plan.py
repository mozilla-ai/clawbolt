"""Tests for the admin plan-change endpoint (PUT /api/admin/users/{id}/plan)."""

from __future__ import annotations

import datetime
import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.app.billing.plans import PLANS
from backend.app.models import Subscription, UsageQuota, User
from tests.multi_user.conftest import open_test_db_session


def _create_user() -> User:
    """Create a minimal User row directly via the OSS session factory."""
    db = open_test_db_session()
    try:
        user = User(
            id=str(uuid.uuid4()),
            user_id=f"google_{uuid.uuid4().hex[:8]}",
            phone="",
            onboarding_complete=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        db.expunge(user)
    finally:
        db.close()
    return user


def _current_period_start() -> datetime.datetime:
    now = datetime.datetime.now(datetime.UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


class TestUpdateUserPlan:
    def test_flips_plan_and_recaps_active_quota(
        self,
        client: TestClient,
        test_subscription: Subscription,
        db_session: Session,
    ) -> None:
        """Free->pro lifts the active month's caps without resetting counters."""
        target = _create_user()
        db_session.add(Subscription(user_id=target.id, role="user", plan="free", status="active"))
        # Pretend the user is partway through their free cap.
        db_session.add(
            UsageQuota(
                user_id=target.id,
                period_start=_current_period_start(),
                messages_used=42,
                messages_limit=PLANS["free"].messages_per_month,
                tokens_used=1_234_567,
                tokens_limit=PLANS["free"].tokens_per_month,
            )
        )
        db_session.commit()

        resp = client.put(f"/api/admin/users/{target.id}/plan", json={"plan": "pro"})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["user_id"] == target.id
        assert data["plan"] == "pro"
        assert data["messages_limit"] == PLANS["pro"].messages_per_month
        assert data["tokens_limit"] == PLANS["pro"].tokens_per_month

        # Subscription row updated, counters preserved, caps lifted on the existing row.
        db_session.expire_all()
        sub = db_session.query(Subscription).filter(Subscription.user_id == target.id).one()
        assert sub.plan == "pro"
        quota = db_session.query(UsageQuota).filter(UsageQuota.user_id == target.id).one()
        assert quota.messages_used == 42
        assert quota.tokens_used == 1_234_567
        assert quota.messages_limit == PLANS["pro"].messages_per_month
        assert quota.tokens_limit == PLANS["pro"].tokens_per_month

    def test_demotion_lowers_caps_on_active_row(
        self,
        client: TestClient,
        test_subscription: Subscription,
        db_session: Session,
    ) -> None:
        """Pro->free tightens caps mid-month; counters survive the demotion."""
        target = _create_user()
        db_session.add(Subscription(user_id=target.id, role="user", plan="pro", status="active"))
        db_session.add(
            UsageQuota(
                user_id=target.id,
                period_start=_current_period_start(),
                messages_used=7,
                messages_limit=PLANS["pro"].messages_per_month,
                tokens_used=500_000,
                tokens_limit=PLANS["pro"].tokens_per_month,
            )
        )
        db_session.commit()

        resp = client.put(f"/api/admin/users/{target.id}/plan", json={"plan": "free"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["messages_limit"] == PLANS["free"].messages_per_month

        db_session.expire_all()
        quota = db_session.query(UsageQuota).filter(UsageQuota.user_id == target.id).one()
        assert quota.messages_used == 7
        assert quota.tokens_used == 500_000
        assert quota.messages_limit == PLANS["free"].messages_per_month
        assert quota.tokens_limit == PLANS["free"].tokens_per_month

    def test_no_active_quota_row_is_fine(
        self,
        client: TestClient,
        test_subscription: Subscription,
        db_session: Session,
    ) -> None:
        """A user with no current-period quota row still has their plan flipped.

        ``get_current_quota`` creates a row at response time, picking up
        the new plan's caps; the apply helper is a no-op when no row
        exists yet and that is fine.
        """
        target = _create_user()
        db_session.add(Subscription(user_id=target.id, role="user", plan="free", status="active"))
        db_session.commit()

        resp = client.put(f"/api/admin/users/{target.id}/plan", json={"plan": "pro"})
        assert resp.status_code == 200
        assert resp.json()["messages_limit"] == PLANS["pro"].messages_per_month

    def test_same_plan_is_noop_and_returns_current_caps(
        self,
        client: TestClient,
        test_subscription: Subscription,
        db_session: Session,
    ) -> None:
        """Setting the plan to its current value returns 200 with the active caps."""
        target = _create_user()
        db_session.add(Subscription(user_id=target.id, role="user", plan="free", status="active"))
        # Active row from before any plan tightening, with the old loose caps.
        db_session.add(
            UsageQuota(
                user_id=target.id,
                period_start=_current_period_start(),
                messages_used=0,
                messages_limit=50_000,
                tokens_used=0,
                tokens_limit=50_000_000,
            )
        )
        db_session.commit()

        resp = client.put(f"/api/admin/users/{target.id}/plan", json={"plan": "free"})
        assert resp.status_code == 200
        # The row is untouched on a no-op: limits reflect the pre-existing row,
        # not the new plan's caps. This is intentional; the next monthly reset
        # picks up the tightened ``free`` caps automatically.
        data = resp.json()
        assert data["plan"] == "free"
        assert data["messages_limit"] == 50_000

    def test_unknown_plan_rejected(
        self,
        client: TestClient,
        test_subscription: Subscription,
        db_session: Session,
    ) -> None:
        target = _create_user()
        db_session.add(Subscription(user_id=target.id, role="user", plan="free", status="active"))
        db_session.commit()

        resp = client.put(f"/api/admin/users/{target.id}/plan", json={"plan": "enterprise"})
        assert resp.status_code == 400
        assert "Unknown plan" in resp.json()["detail"]

        # Subscription row unchanged after the rejected request.
        db_session.expire_all()
        sub = db_session.query(Subscription).filter(Subscription.user_id == target.id).one()
        assert sub.plan == "free"

    def test_returns_404_when_user_missing(
        self,
        client: TestClient,
        test_subscription: Subscription,
    ) -> None:
        resp = client.put(f"/api/admin/users/{uuid.uuid4()}/plan", json={"plan": "pro"})
        assert resp.status_code == 404

    def test_non_admin_blocked(
        self,
        client: TestClient,
        test_user: User,
        db_session: Session,
    ) -> None:
        """Caller subscription with role=user gets 403 before reaching the route."""
        db_session.add(
            Subscription(user_id=test_user.id, role="user", plan="free", status="active")
        )
        db_session.commit()

        resp = client.put(f"/api/admin/users/{test_user.id}/plan", json={"plan": "pro"})
        assert resp.status_code == 403

    def test_self_plan_change_blocked(
        self,
        client: TestClient,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """Admin cannot change their own plan."""
        # test_subscription has role=admin, so the admin dep passes.
        # Hitting the admin's own user_id triggers the self-guard.
        resp = client.put(f"/api/admin/users/{test_user.id}/plan", json={"plan": "pro"})
        assert resp.status_code == 400
        assert "cannot change their own plan" in resp.json()["detail"].lower()
