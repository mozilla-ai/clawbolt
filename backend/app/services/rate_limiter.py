"""Simple in-memory rate limiter for webhook endpoints.

This is a per-process in-memory implementation using a sliding window counter.
It is suitable for single-process deployments. For production deployments with
multiple workers/processes, replace this with a distributed rate limiter backed
by Redis or similar shared storage.
"""

import time
from collections import defaultdict

from fastapi import HTTPException, Request

from backend.app.config import settings


class InMemoryRateLimiter:
    """Sliding-window rate limiter that tracks request counts per key (IP address).

    Args:
        max_requests: Maximum number of requests allowed within the window.
        window_seconds: Duration of the sliding window in seconds.
    """

    def __init__(self, max_requests: int = 30, window_seconds: int = 60) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        # key -> list of timestamps
        self._requests: dict[str, list[float]] = defaultdict(list)

    def _get_client_ip(self, request: Request) -> str:
        """Extract client IP from request, respecting X-Forwarded-For."""
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            # First IP in the chain is the original client
            return forwarded.split(",")[0].strip()
        client = request.client
        if client:
            return client.host
        return "unknown"

    def _prune(self, key: str, now: float) -> None:
        """Remove timestamps outside the current window."""
        cutoff = now - self.window_seconds
        timestamps = self._requests[key]
        # Find first index within the window
        i = 0
        while i < len(timestamps) and timestamps[i] < cutoff:
            i += 1
        if i > 0:
            self._requests[key] = timestamps[i:]

    def check(self, request: Request) -> None:
        """Check rate limit for the given request. Raises 429 if exceeded."""
        key = self._get_client_ip(request)
        now = time.monotonic()
        self._prune(key, now)

        if len(self._requests[key]) >= self.max_requests:
            raise HTTPException(status_code=429, detail="Rate limit exceeded")

        self._requests[key].append(now)

    def reset(self) -> None:
        """Clear all tracked requests. Useful for testing."""
        self._requests.clear()


# Singleton instance used by the webhook endpoint.
# 30 requests per 60 seconds per IP address.
webhook_rate_limiter = InMemoryRateLimiter(
    max_requests=settings.webhook_rate_limit_max_requests,
    window_seconds=settings.webhook_rate_limit_window_seconds,
)


def check_webhook_rate_limit(request: Request) -> None:
    """FastAPI dependency that enforces rate limiting on the webhook endpoint."""
    webhook_rate_limiter.check(request)
