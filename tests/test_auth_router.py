"""Tests for the auth router endpoint (GET /api/auth/config)."""

from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.app.auth.base import AuthBackend


class _FakeAuthBackend(AuthBackend):
    """Minimal concrete AuthBackend for testing the premium-plugin path."""

    def get_auth_config(self) -> dict[str, Any]:
        return {"method": "firebase", "required": True, "project_id": "my-project"}

    def authenticate_login(self, db: Any, credentials: dict[str, str]) -> Any:
        raise NotImplementedError


def test_auth_config_returns_none_when_no_backend(client: TestClient) -> None:
    """When no premium plugin is loaded, the endpoint returns method=none."""
    response = client.get("/api/auth/config")
    assert response.status_code == 200
    data = response.json()
    assert data["method"] == "none"
    assert data["required"] is False


def test_auth_config_response_structure_without_backend(client: TestClient) -> None:
    """The no-backend response contains exactly the expected keys."""
    response = client.get("/api/auth/config")
    data = response.json()
    assert set(data.keys()) == {"method", "required"}


def test_auth_config_delegates_to_backend(client: TestClient) -> None:
    """When a premium auth backend is loaded, its get_auth_config() is returned."""
    fake_backend = _FakeAuthBackend()
    with patch("backend.app.routers.auth.get_auth_backend", return_value=fake_backend):
        response = client.get("/api/auth/config")
    assert response.status_code == 200
    data = response.json()
    assert data == {"method": "firebase", "required": True, "project_id": "my-project"}


def test_auth_config_content_type(client: TestClient) -> None:
    """The endpoint returns application/json content type."""
    response = client.get("/api/auth/config")
    assert "application/json" in response.headers["content-type"]
