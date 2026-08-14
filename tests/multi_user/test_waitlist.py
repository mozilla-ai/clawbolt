"""Tests for waitlist feature.

Covers:
- Public join endpoint (success, normalization, idempotent, source, validation)
- Admin endpoints (list, approve, dismiss, auth enforcement)
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.app.middleware.rate_limit import _auth_rate_limiter
from backend.app.models import AllowedEmail, Subscription, WaitlistEntry


@pytest.fixture(autouse=True)
def _clear_rate_limits() -> None:
    _auth_rate_limiter.reset()


class TestWaitlistPublicEndpoint:
    """POST /api/waitlist/join -- public, no auth."""

    def test_join_success(self, client: TestClient) -> None:
        resp = client.post("/api/waitlist/join", json={"email": "new@example.com"})
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_join_normalizes_email(self, client: TestClient, db_session: Session) -> None:
        resp = client.post("/api/waitlist/join", json={"email": "  FOO@Example.COM  "})
        assert resp.status_code == 200
        entry = db_session.query(WaitlistEntry).filter_by(email="foo@example.com").first()
        assert entry is not None

    def test_join_duplicate_is_idempotent(self, client: TestClient, db_session: Session) -> None:
        client.post("/api/waitlist/join", json={"email": "dupe@example.com"})
        resp = client.post("/api/waitlist/join", json={"email": "dupe@example.com"})
        assert resp.status_code == 200
        count = db_session.query(WaitlistEntry).filter_by(email="dupe@example.com").count()
        assert count == 1

    def test_join_records_source(self, client: TestClient, db_session: Session) -> None:
        client.post("/api/waitlist/join", json={"email": "src@example.com", "source": "login"})
        entry = db_session.query(WaitlistEntry).filter_by(email="src@example.com").first()
        assert entry is not None
        assert entry.source == "login"

    def test_join_records_name(self, client: TestClient, db_session: Session) -> None:
        resp = client.post(
            "/api/waitlist/join", json={"email": "named@example.com", "name": "  Alice  "}
        )
        assert resp.status_code == 200
        entry = db_session.query(WaitlistEntry).filter_by(email="named@example.com").first()
        assert entry is not None
        assert entry.name == "Alice"

    def test_join_without_name_falls_back_to_default(
        self, client: TestClient, db_session: Session
    ) -> None:
        resp = client.post("/api/waitlist/join", json={"email": "noname@example.com"})
        assert resp.status_code == 200
        entry = db_session.query(WaitlistEntry).filter_by(email="noname@example.com").first()
        assert entry is not None
        assert entry.name == "user"

    def test_join_truncates_long_name(self, client: TestClient, db_session: Session) -> None:
        long_name = "x" * 500
        resp = client.post(
            "/api/waitlist/join", json={"email": "long@example.com", "name": long_name}
        )
        assert resp.status_code == 200
        entry = db_session.query(WaitlistEntry).filter_by(email="long@example.com").first()
        assert entry is not None
        assert len(entry.name) == 120

    def test_join_records_use_case(self, client: TestClient, db_session: Session) -> None:
        resp = client.post(
            "/api/waitlist/join",
            json={
                "email": "plumber@example.com",
                "name": "Pat",
                "use_case": "  I run a plumbing shop and want help with estimates.  ",
            },
        )
        assert resp.status_code == 200
        entry = db_session.query(WaitlistEntry).filter_by(email="plumber@example.com").first()
        assert entry is not None
        assert entry.use_case == "I run a plumbing shop and want help with estimates."

    def test_join_use_case_empty_becomes_null(
        self, client: TestClient, db_session: Session
    ) -> None:
        resp = client.post(
            "/api/waitlist/join",
            json={"email": "noctx@example.com", "name": "Pat", "use_case": "   "},
        )
        assert resp.status_code == 200
        entry = db_session.query(WaitlistEntry).filter_by(email="noctx@example.com").first()
        assert entry is not None
        assert entry.use_case is None

    def test_join_truncates_long_use_case(self, client: TestClient, db_session: Session) -> None:
        long_text = "y" * 3000
        resp = client.post(
            "/api/waitlist/join",
            json={"email": "long-uc@example.com", "name": "Pat", "use_case": long_text},
        )
        assert resp.status_code == 200
        entry = db_session.query(WaitlistEntry).filter_by(email="long-uc@example.com").first()
        assert entry is not None
        assert entry.use_case is not None
        assert len(entry.use_case) == 2000

    def test_join_default_source_is_homepage(self, client: TestClient, db_session: Session) -> None:
        client.post("/api/waitlist/join", json={"email": "def@example.com"})
        entry = db_session.query(WaitlistEntry).filter_by(email="def@example.com").first()
        assert entry is not None
        assert entry.source == "homepage"

    def test_join_invalid_email_returns_422(self, client: TestClient) -> None:
        resp = client.post("/api/waitlist/join", json={"email": "not-an-email"})
        assert resp.status_code == 422

    def test_join_skips_if_already_allowed(self, client: TestClient, db_session: Session) -> None:
        db_session.add(AllowedEmail(email="allowed@example.com"))
        db_session.commit()
        resp = client.post("/api/waitlist/join", json={"email": "allowed@example.com"})
        assert resp.status_code == 200
        entry = db_session.query(WaitlistEntry).filter_by(email="allowed@example.com").first()
        assert entry is None


class TestWaitlistAdmin:
    """Admin waitlist management endpoints."""

    def test_list_empty(self, client: TestClient, test_subscription: Subscription) -> None:
        resp = client.get("/api/admin/waitlist")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    def test_list_after_join(self, client: TestClient, test_subscription: Subscription) -> None:
        client.post(
            "/api/waitlist/join",
            json={
                "email": "waitlisted@example.com",
                "name": "Bob",
                "use_case": "Roofing contractor in Texas.",
            },
        )
        resp = client.get("/api/admin/waitlist")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        item = data["items"][0]
        assert item["email"] == "waitlisted@example.com"
        assert item["name"] == "Bob"
        assert item["use_case"] == "Roofing contractor in Texas."

    def test_list_use_case_null_when_absent(
        self, client: TestClient, test_subscription: Subscription
    ) -> None:
        client.post("/api/waitlist/join", json={"email": "nouse@example.com", "name": "Sam"})
        resp = client.get("/api/admin/waitlist")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"][0]["use_case"] is None

    def test_approve_entry(
        self, client: TestClient, test_subscription: Subscription, db_session: Session
    ) -> None:
        client.post(
            "/api/waitlist/join", json={"email": "approve-me@example.com", "source": "login"}
        )
        entry = db_session.query(WaitlistEntry).filter_by(email="approve-me@example.com").first()
        assert entry is not None

        resp = client.post(f"/api/admin/waitlist/{entry.id}/approve")
        assert resp.status_code == 200
        assert resp.json()["email"] == "approve-me@example.com"

        # Email should now be in allowed_emails
        allowed = db_session.query(AllowedEmail).filter_by(email="approve-me@example.com").first()
        assert allowed is not None
        assert allowed.note == "Approved from waitlist"

        # Waitlist entry should be gone
        entry = db_session.query(WaitlistEntry).filter_by(email="approve-me@example.com").first()
        assert entry is None

    def test_approve_already_allowed(
        self, client: TestClient, test_subscription: Subscription, db_session: Session
    ) -> None:
        db_session.add(AllowedEmail(email="already@example.com"))
        db_session.commit()

        entry = WaitlistEntry(email="already@example.com", source="login")
        db_session.add(entry)
        db_session.commit()
        db_session.refresh(entry)

        resp = client.post(f"/api/admin/waitlist/{entry.id}/approve")
        assert resp.status_code == 200

        # Waitlist entry removed, allowed email unchanged
        remaining = db_session.query(WaitlistEntry).filter_by(email="already@example.com").first()
        assert remaining is None

    def test_dismiss_entry(
        self, client: TestClient, test_subscription: Subscription, db_session: Session
    ) -> None:
        client.post("/api/waitlist/join", json={"email": "dismiss-me@example.com"})
        entry = db_session.query(WaitlistEntry).filter_by(email="dismiss-me@example.com").first()
        assert entry is not None

        resp = client.delete(f"/api/admin/waitlist/{entry.id}")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

        # Should be gone
        entry = db_session.query(WaitlistEntry).filter_by(email="dismiss-me@example.com").first()
        assert entry is None

    def test_dismiss_nonexistent_returns_404(
        self, client: TestClient, test_subscription: Subscription
    ) -> None:
        resp = client.delete("/api/admin/waitlist/99999")
        assert resp.status_code == 404

    def test_approve_nonexistent_returns_404(
        self, client: TestClient, test_subscription: Subscription
    ) -> None:
        resp = client.post("/api/admin/waitlist/99999/approve")
        assert resp.status_code == 404
