"""Tests for the /api/auth/config endpoint and OAuthBackend.get_auth_config().

These tests guard the contract between the backend auth config and the
frontend isPremiumAuth() check. A mismatch here silently breaks the
entire premium frontend (homepage routing, login flow, session restore).
"""

from collections.abc import Iterator
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from backend.app.auth import loader as auth_loader
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
        """GET /api/auth/config returns the multi-user auth config, not OSS defaults."""
        # Patch the loader so the endpoint uses OAuthBackend regardless of
        # what an earlier test left in the loader's module-level cache.
        with patch(
            "backend.app.routers.auth.get_auth_backend",
            return_value=OAuthBackend(),
        ):
            resp = client.get("/api/auth/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["method"] == "oauth_google"
        assert data["required"] is True


class TestAuthBackendResolution:
    """``AUTH_MODE`` alone decides the backend, now that plugins are gone."""

    @pytest.fixture(autouse=True)
    def _reset_backend(self) -> Iterator[None]:
        # Both sides: the cache is module-level, so a resolution made under
        # a patched mode must not outlive the test that made it.
        auth_loader.reset_auth_backend()
        yield
        auth_loader.reset_auth_backend()

    def test_multi_user_resolves_oauth_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(auth_loader.settings, "auth_mode", "multi_user")
        assert isinstance(auth_loader.get_auth_backend(), OAuthBackend)

    def test_single_user_resolves_no_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No backend is how ``/api/auth/config`` tells the SPA no login is required."""
        monkeypatch.setattr(auth_loader.settings, "auth_mode", "single_user")
        assert auth_loader.get_auth_backend() is None
