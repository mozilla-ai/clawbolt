"""Tests for webhook rate limiting."""

import asyncio
import time
from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from backend.app.agent.core import AgentResponse
from backend.app.agent.file_store import ContractorData, get_contractor_store, reset_stores
from backend.app.auth.dependencies import get_current_user
from backend.app.config import settings
from backend.app.main import app
from backend.app.services.messaging import MessagingService, get_messaging_service
from backend.app.services.rate_limiter import InMemoryRateLimiter, check_webhook_rate_limit
from tests.mocks.telegram import make_telegram_update_payload

_MOCK_AGENT_RESPONSE = AgentResponse(reply_text="Mock reply")
_PATCH_HANDLE = "backend.app.agent.ingestion.handle_inbound_message"


def _make_scope(
    client_ip: str = "127.0.0.1",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> dict:
    """Build a minimal ASGI scope for testing."""
    return {
        "type": "http",
        "method": "POST",
        "path": "/test",
        "headers": headers or [],
        "query_string": b"",
        "server": ("testserver", 80),
        "client": (client_ip, 12345),
    }


@pytest.fixture()
def _rate_limited_client(tmp_path: object) -> Generator[TestClient]:
    """TestClient that does NOT override the rate limiter, so rate limiting is active."""
    with patch.object(settings, "contractor_data_dir", str(tmp_path)):
        reset_stores()

        store = get_contractor_store()
        contractor = asyncio.get_event_loop().run_until_complete(
            store.create(
                user_id="rl-test-user",
                name="RL Test",
                phone="+15559999999",
                channel_identifier="777777",
                preferred_channel="telegram",
            )
        )

        def _override_get_current_user() -> ContractorData:
            return contractor

        mock_messaging = MagicMock(spec=MessagingService)
        mock_messaging.send_text = AsyncMock(return_value="mock_msg_id")
        mock_messaging.send_media = AsyncMock(return_value="mock_msg_id")
        mock_messaging.send_message = AsyncMock(return_value="mock_msg_id")
        mock_messaging.send_typing_indicator = AsyncMock()
        mock_messaging.download_media = AsyncMock()

        def _override_get_messaging_service() -> Generator[MessagingService]:
            yield mock_messaging

        # Reset the rate limiter before each test that uses this fixture
        from backend.app.services.rate_limiter import webhook_rate_limiter

        webhook_rate_limiter.reset()

        app.dependency_overrides[get_current_user] = _override_get_current_user
        app.dependency_overrides[get_messaging_service] = _override_get_messaging_service
        # Explicitly remove rate limiter override so the real one is used
        app.dependency_overrides.pop(check_webhook_rate_limit, None)

        with (
            patch("backend.app.main._verify_llm_settings", new_callable=AsyncMock),
            patch("backend.app.agent.heartbeat.heartbeat_scheduler.start"),
            TestClient(app) as c,
        ):
            yield c

        app.dependency_overrides.clear()
        webhook_rate_limiter.reset()
        reset_stores()


class TestInMemoryRateLimiter:
    """Unit tests for the InMemoryRateLimiter class."""

    def test_allows_requests_under_limit(self) -> None:
        """Requests under the max limit should be allowed."""
        limiter = InMemoryRateLimiter(max_requests=5, window_seconds=60)

        for _ in range(5):
            request = Request(_make_scope())
            limiter.check(request)  # Should not raise

    def test_blocks_requests_over_limit(self) -> None:
        """Requests exceeding the max limit should raise 429."""
        limiter = InMemoryRateLimiter(max_requests=3, window_seconds=60)

        for _ in range(3):
            request = Request(_make_scope())
            limiter.check(request)

        # 4th request should be blocked
        request = Request(_make_scope())
        with pytest.raises(HTTPException) as exc_info:
            limiter.check(request)
        assert exc_info.value.status_code == 429

    def test_window_expiry_allows_new_requests(self) -> None:
        """After the window expires, new requests should be allowed."""
        limiter = InMemoryRateLimiter(max_requests=2, window_seconds=1)

        # Fill the window
        for _ in range(2):
            request = Request(_make_scope())
            limiter.check(request)

        # Wait for the window to expire
        time.sleep(1.1)

        # Should be allowed again
        request = Request(_make_scope())
        limiter.check(request)  # Should not raise

    def test_different_ips_tracked_independently(self) -> None:
        """Different IPs should have independent rate limits."""
        limiter = InMemoryRateLimiter(max_requests=2, window_seconds=60)

        # IP A uses 2 requests
        for _ in range(2):
            limiter.check(Request(_make_scope(client_ip="10.0.0.1")))

        # IP B should still be allowed
        limiter.check(Request(_make_scope(client_ip="10.0.0.2")))  # Should not raise

    def test_x_forwarded_for_trusted_when_trust_proxy_enabled(self) -> None:
        """When trust_proxy is True, X-Forwarded-For should be used for rate limiting."""
        limiter = InMemoryRateLimiter(max_requests=2, window_seconds=60)

        with patch("backend.app.services.rate_limiter.settings.rate_limit_trust_proxy", True):
            # Requests from different socket IPs but same X-Forwarded-For
            for i in range(2):
                scope = _make_scope(
                    client_ip=f"10.0.0.{i}",
                    headers=[(b"x-forwarded-for", b"203.0.113.50, 70.41.3.18")],
                )
                limiter.check(Request(scope))

            # 3rd request from same forwarded IP should be blocked
            scope = _make_scope(
                client_ip="10.0.0.99",
                headers=[(b"x-forwarded-for", b"203.0.113.50, 70.41.3.18")],
            )
            with pytest.raises(HTTPException) as exc_info:
                limiter.check(Request(scope))
            assert exc_info.value.status_code == 429

    def test_x_forwarded_for_ignored_when_trust_proxy_disabled(self) -> None:
        """When trust_proxy is False, X-Forwarded-For should be ignored."""
        limiter = InMemoryRateLimiter(max_requests=2, window_seconds=60)

        with patch("backend.app.services.rate_limiter.settings.rate_limit_trust_proxy", False):
            # Requests from different socket IPs with same X-Forwarded-For
            for i in range(3):
                scope = _make_scope(
                    client_ip=f"10.0.0.{i}",
                    headers=[(b"x-forwarded-for", b"203.0.113.50, 70.41.3.18")],
                )
                limiter.check(Request(scope))  # Should not raise: each socket IP is independent

    def test_expired_keys_removed_from_dict(self) -> None:
        """After all timestamps expire and the key is pruned, it should be removed."""
        limiter = InMemoryRateLimiter(max_requests=5, window_seconds=1)

        limiter.check(Request(_make_scope(client_ip="10.0.0.1")))
        assert "10.0.0.1" in limiter._requests

        # Wait for the window to expire
        time.sleep(1.1)

        # Manually prune the expired key — this simulates what happens when
        # the same IP makes another request after the window expires
        limiter._prune("10.0.0.1", time.monotonic())
        assert "10.0.0.1" not in limiter._requests

    def test_reset_clears_state(self) -> None:
        """reset() should clear all tracked requests."""
        limiter = InMemoryRateLimiter(max_requests=2, window_seconds=60)

        for _ in range(2):
            limiter.check(Request(_make_scope()))

        limiter.reset()

        # Should be allowed again after reset
        limiter.check(Request(_make_scope()))  # Should not raise


class TestWebhookRateLimiting:
    """Integration tests: rate limiting on the actual webhook endpoint."""

    def test_requests_under_limit_succeed(self, _rate_limited_client: TestClient) -> None:
        """Requests under the rate limit should return 200."""
        with patch(_PATCH_HANDLE, new_callable=AsyncMock, return_value=_MOCK_AGENT_RESPONSE):
            for i in range(5):
                payload = make_telegram_update_payload(
                    chat_id=777777,
                    text=f"Message {i}",
                    message_id=1000 + i,
                )
                response = _rate_limited_client.post("/api/webhooks/telegram", json=payload)
                assert response.status_code == 200

    def test_requests_over_limit_return_429(self, _rate_limited_client: TestClient) -> None:
        """Requests exceeding the rate limit should return 429."""
        from backend.app.services.rate_limiter import webhook_rate_limiter

        # Use a small limit for testing
        original_max = webhook_rate_limiter.max_requests
        webhook_rate_limiter.max_requests = 3

        try:
            with patch(_PATCH_HANDLE, new_callable=AsyncMock, return_value=_MOCK_AGENT_RESPONSE):
                # First 3 should succeed
                for i in range(3):
                    payload = make_telegram_update_payload(
                        chat_id=777777,
                        text=f"Message {i}",
                        message_id=2000 + i,
                    )
                    response = _rate_limited_client.post("/api/webhooks/telegram", json=payload)
                    assert response.status_code == 200

                # 4th should be rate-limited
                payload = make_telegram_update_payload(
                    chat_id=777777,
                    text="Too many",
                    message_id=2003,
                )
                response = _rate_limited_client.post("/api/webhooks/telegram", json=payload)
                assert response.status_code == 429
        finally:
            webhook_rate_limiter.max_requests = original_max
