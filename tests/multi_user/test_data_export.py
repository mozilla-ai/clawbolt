"""Tests for GDPR data export endpoint."""

import asyncio

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.app.agent.file_store import (
    get_memory_store,
    get_session_store,
)
from backend.app.models import Subscription, UsageQuota, User


class TestDataExport:
    def test_export_returns_profile(self, client: TestClient, test_user: User) -> None:
        """Export should include user profile data."""
        resp = client.get("/api/account/export")
        assert resp.status_code == 200
        data = resp.json()
        assert "profile" in data
        assert data["profile"]["id"] == test_user.id

    def test_export_returns_all_sections(self, client: TestClient, test_user: User) -> None:
        """Export should include all expected data sections."""
        resp = client.get("/api/account/export")
        data = resp.json()
        expected_keys = [
            "profile",
            "memories",
            "conversations",
            "heartbeat_text",
            "llm_usage",
            "subscription",
            "usage_quotas",
        ]
        for key in expected_keys:
            assert key in data, f"Missing section: {key}"

    async def test_export_includes_memories(
        self,
        client: TestClient,
        test_user: User,
    ) -> None:
        """Export should include the user's memories."""
        mem_store = get_memory_store(test_user.id)
        await mem_store.write_memory_async("## Pricing\n- Hourly rate: $75")

        resp = client.get("/api/account/export")
        data = resp.json()
        assert "Hourly rate" in data["memories"]

    def test_export_includes_conversations_with_messages(
        self,
        client: TestClient,
        test_user: User,
    ) -> None:
        """Export should include conversations with nested messages."""
        session_store = get_session_store(test_user.id)
        session, _ = asyncio.run(session_store.get_or_create_session())
        asyncio.run(session_store.add_message(session, direction="inbound", body="Hello there"))

        resp = client.get("/api/account/export")
        data = resp.json()
        assert len(data["conversations"]) == 1
        assert len(data["conversations"][0]["messages"]) == 1
        assert data["conversations"][0]["messages"][0]["body"] == "Hello there"

    def test_export_includes_subscription(
        self,
        client: TestClient,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """Export should include subscription data."""
        resp = client.get("/api/account/export")
        data = resp.json()
        assert data["subscription"] is not None
        assert data["subscription"]["plan"] == "free"

    def test_export_includes_usage_quotas(
        self,
        client: TestClient,
        test_user: User,
        test_quota: UsageQuota,
        db_session: Session,
    ) -> None:
        """Export should include usage quota data."""
        # Commit the change so the route's async DB connection sees it under
        # READ COMMITTED. With the route on ``Depends(get_async_db)``, sync
        # in-memory mutations from the fixture connection are no longer
        # visible to the route's separate async connection.
        test_quota.messages_used = 10
        db_session.commit()

        resp = client.get("/api/account/export")
        data = resp.json()
        assert len(data["usage_quotas"]) == 1
        assert data["usage_quotas"][0]["messages_used"] == 10

    def test_export_empty_when_no_data(self, client: TestClient, test_user: User) -> None:
        """Export should return empty lists for sections with no data."""
        resp = client.get("/api/account/export")
        data = resp.json()
        assert data["memories"] == ""
        assert data["conversations"] == []
        assert data["subscription"] is None
