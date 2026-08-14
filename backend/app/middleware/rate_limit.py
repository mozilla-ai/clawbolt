"""Rate limiter for the sign-in endpoints.

Delegates to ``InMemoryRateLimiter`` to avoid a duplicate implementation.
Uses a separate instance with configurable limits (default: 10 req/60s)
compared to the webhook limiter (30 req/60s).
"""

from fastapi import Request

from backend.app.config import settings
from backend.app.services.rate_limiter import InMemoryRateLimiter

_auth_rate_limiter = InMemoryRateLimiter(
    max_requests=settings.auth_rate_limit_max_requests,
    window_seconds=settings.auth_rate_limit_window_seconds,
)


def check_rate_limit(request: Request) -> None:
    """Check rate limit for the request's client IP.

    Raises HTTPException(429) if the limit is exceeded.
    """
    _auth_rate_limiter.check(request)
