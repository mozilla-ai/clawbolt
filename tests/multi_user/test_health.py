"""Tests for the detailed health endpoint.

``/api/health`` (single-user, OSS-owned) answers the same status
question. This one adds process uptime, which is what tells "still
degraded" apart from "just restarted".
"""

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from backend.app.models import Subscription


class TestHealthEndpoint:
    def test_healthy_response(self, client: TestClient, test_subscription: Subscription) -> None:
        """Health endpoint should return healthy when DB is reachable."""
        resp = client.get("/api/health/detail")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert data["database"] == "connected"
        assert "uptime_seconds" in data
        assert "timestamp" in data

    def test_degraded_when_db_unreachable(
        self, client: TestClient, test_subscription: Subscription
    ) -> None:
        """Health endpoint should return degraded when DB fails."""
        broken_db = MagicMock()
        broken_db.execute = AsyncMock(side_effect=Exception("connection refused"))

        async def _broken_get_async_db() -> AsyncGenerator[MagicMock]:
            yield broken_db

        from backend.app.database import get_async_db
        from tests.multi_user.conftest import MULTI_USER_APP as app

        app.dependency_overrides[get_async_db] = _broken_get_async_db
        try:
            resp = client.get("/api/health/detail")
        finally:
            del app.dependency_overrides[get_async_db]

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "degraded"
        assert data["database"] == "unreachable"
