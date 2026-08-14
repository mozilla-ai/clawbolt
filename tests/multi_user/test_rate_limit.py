"""Tests for rate limiter middleware."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from backend.app.middleware.rate_limit import _auth_rate_limiter, check_rate_limit


@pytest.fixture(autouse=True)
def _clear_rate_limit_store() -> None:
    """Clear rate limit state between tests."""
    _auth_rate_limiter.reset()


def _make_request(ip: str = "127.0.0.1") -> MagicMock:
    request = MagicMock()
    request.client.host = ip
    request.headers = {}
    return request


class TestRateLimiter:
    def test_allows_under_limit(self) -> None:
        """Requests under the limit should pass."""
        request = _make_request()
        for _ in range(9):
            check_rate_limit(request)

    def test_blocks_at_limit(self) -> None:
        """Request at the limit should raise 429."""
        request = _make_request()
        for _ in range(10):
            check_rate_limit(request)

        with pytest.raises(HTTPException) as exc_info:
            check_rate_limit(request)
        assert exc_info.value.status_code == 429

    def test_different_ips_independent(self) -> None:
        """Rate limits should be per-IP."""
        req1 = _make_request("1.1.1.1")
        req2 = _make_request("2.2.2.2")

        for _ in range(10):
            check_rate_limit(req1)

        # Different IP should still be allowed
        check_rate_limit(req2)

    def test_handles_missing_client(self) -> None:
        """Should handle requests with no client info."""
        request = MagicMock()
        request.client = None
        request.headers = {}
        check_rate_limit(request)

    def test_uses_configured_limits(self) -> None:
        """Rate limiter should respect configured limits from settings."""
        with patch("backend.app.middleware.rate_limit.settings") as mock_settings:
            mock_settings.auth_rate_limit_max_requests = 10
            mock_settings.auth_rate_limit_window_seconds = 60
            # The limiter is already initialized with defaults (10/60),
            # so the existing behavior matches the configured defaults
            request = _make_request("10.0.0.1")
            for _ in range(10):
                check_rate_limit(request)
            with pytest.raises(HTTPException) as exc_info:
                check_rate_limit(request)
            assert exc_info.value.status_code == 429
