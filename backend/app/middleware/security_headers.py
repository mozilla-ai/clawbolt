"""Security headers middleware: CSP, HSTS, X-Frame-Options, Permissions-Policy.

Implemented as pure ASGI middleware to avoid BaseHTTPMiddleware issues
with streaming responses.
"""

from starlette.types import ASGIApp, Message, Receive, Scope, Send

SECURITY_HEADERS: list[tuple[bytes, bytes]] = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"x-xss-protection", b"1; mode=block"),
    (b"strict-transport-security", b"max-age=31536000; includeSubDomains"),
    (
        b"content-security-policy",
        b"default-src 'self'; "
        b"script-src 'self'; "
        b"style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        b"img-src 'self' data: blob:; "
        b"font-src 'self' https://fonts.gstatic.com; "
        b"connect-src 'self'; "
        b"frame-ancestors 'none'",
    ),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (
        b"permissions-policy",
        b"camera=(), microphone=(), geolocation=(), payment=(), usb=(), "
        b"accelerometer=(), gyroscope=(), magnetometer=()",
    ),
]


class SecurityHeadersMiddleware:
    """Add security headers to all HTTP responses (pure ASGI)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(SECURITY_HEADERS)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)
