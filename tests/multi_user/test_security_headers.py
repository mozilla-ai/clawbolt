"""Tests for security headers middleware (regression: #24)."""

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from backend.app.middleware.security_headers import SecurityHeadersMiddleware


def _homepage(request: object) -> PlainTextResponse:
    return PlainTextResponse("ok")


class TestSecurityHeaders:
    def test_response_includes_security_headers(self) -> None:
        """All security headers must be present on HTTP responses."""
        app = Starlette(routes=[Route("/", _homepage)])
        app.add_middleware(SecurityHeadersMiddleware)  # ty: ignore[invalid-argument-type]
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/")
        assert response.status_code == 200
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["x-xss-protection"] == "1; mode=block"
        assert "max-age=31536000" in response.headers["strict-transport-security"]
        csp = response.headers["content-security-policy"]
        assert "default-src 'self'" in csp
        assert "script-src 'self'" in csp
        assert "img-src 'self' data: blob:" in csp
        assert "frame-ancestors 'none'" in csp
        assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
        assert "camera=()" in response.headers["permissions-policy"]
        assert "microphone=()" in response.headers["permissions-policy"]
        assert "geolocation=()" in response.headers["permissions-policy"]

    def test_middleware_is_pure_asgi(self) -> None:
        """SecurityHeadersMiddleware must not inherit from BaseHTTPMiddleware."""
        from starlette.middleware.base import BaseHTTPMiddleware

        assert not issubclass(SecurityHeadersMiddleware, BaseHTTPMiddleware)
