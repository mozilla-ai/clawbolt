"""Tests for the /api/auth/config endpoint and OAuthBackend.get_auth_config().

These tests guard the contract between the backend auth config and the
frontend isPremiumAuth() check. A mismatch here silently breaks the
entire premium frontend (homepage routing, login flow, session restore).
"""

from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.app.auth.oauth_backend import OAuthBackend


class TestAuthConfigContract:
    """Verify the auth config matches what the frontend expects."""

    def test_method_is_oauth_google(self) -> None:
        """Frontend isPremiumAuth() checks method === 'oauth_google'."""
        backend = OAuthBackend()
        config = backend.get_auth_config()
        assert config["method"] == "oauth_google"

    def test_required_is_true(self) -> None:
        """Frontend AuthContext skips premium flow without required: true."""
        backend = OAuthBackend()
        config = backend.get_auth_config()
        assert config["required"] is True

    def test_has_google_client_id(self) -> None:
        """Frontend needs google_client_id for display purposes."""
        backend = OAuthBackend()
        config = backend.get_auth_config()
        assert "google_client_id" in config

    def test_endpoint_returns_premium_config(self, client: TestClient) -> None:
        """GET /api/auth/config returns the premium auth config, not OSS defaults."""
        # Patch the loader so the endpoint uses OAuthBackend regardless of
        # whether PREMIUM_PLUGIN env var is set (it isn't in CI).
        with patch(
            "backend.app.routers.auth.get_auth_backend",
            return_value=OAuthBackend(),
        ):
            resp = client.get("/api/auth/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["method"] == "oauth_google"
        assert data["required"] is True
