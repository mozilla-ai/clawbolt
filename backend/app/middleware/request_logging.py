"""Request logging middleware.

Sets the per-request correlation ID context, logs method/path/status/duration
with that ID attached, and echoes ``X-Request-ID`` back on the response so
clients can quote it in support tickets.
"""

import logging
import re
import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from backend.app.observability import (
    new_request_id,
    request_id_var,
)

logger = logging.getLogger(__name__)

_HEADER_NAME = b"x-request-id"

# A trusted ID is alphanumeric plus a few separators, max 128 chars. Anything
# else gets discarded and replaced with a fresh ID, so a malicious client
# cannot forge log entries via CRLF injection or smuggle bytes into the
# response header.
_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

# Paths that fire on a fixed timer from infrastructure (admin tab deploy
# poll, container healthcheck, platform liveness probe) and produce zero
# diagnostic value at INFO. Skipping them keeps the access log focused on
# real traffic; correlation IDs are still set, so anything the handler
# itself logs remains attributable via X-Request-ID. Failures on these
# paths still surface through the handler's own error/warning logs and
# through the platform's healthcheck status, so we do not lose signal.
_SKIP_LOG_PATHS = frozenset(
    {
        "/api/admin/version",  # admin overview deploy-detect poll, 60s
        "/api/health",  # Dockerfile HEALTHCHECK, 30s
        "/api/health/live",  # platform liveness probe
    }
)


class RequestLoggingMiddleware:
    """Pure ASGI middleware for request correlation, optionally with an access log.

    The correlation ID is always set and echoed, because downstream log
    lines are attributed through it. ``log_timing`` controls only whether
    this middleware emits its own per-request line.
    """

    def __init__(self, app: ASGIApp, log_timing: bool = True) -> None:
        self.app = app
        self.log_timing = log_timing

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "?")
        path = scope.get("path", "?")
        start = time.monotonic()
        status_code = 0

        # Reuse upstream-set X-Request-ID if present and well-formed, else
        # mint one. Validating the pattern is what prevents header/log
        # injection from a hostile client.
        rid = ""
        for name, value in scope.get("headers", []):
            if name == _HEADER_NAME:
                candidate = value.decode("ascii", errors="ignore").strip()
                if _ID_PATTERN.match(candidate):
                    rid = candidate
                break
        if not rid:
            rid = new_request_id()

        token = request_id_var.set(rid)

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message.get("status", 0)
                # Drop any X-Request-ID set downstream so we end up with
                # exactly one header (avoid duplicate-header confusion).
                headers = [(n, v) for n, v in message.get("headers", []) if n != _HEADER_NAME]
                headers.append((_HEADER_NAME, rid.encode("ascii")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            if self.log_timing and path not in _SKIP_LOG_PATHS:
                duration_ms = (time.monotonic() - start) * 1000
                logger.info(
                    "%s %s %d %.1fms",
                    method,
                    path,
                    status_code,
                    duration_ms,
                )
            request_id_var.reset(token)
