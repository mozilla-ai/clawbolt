"""Tests for user profile endpoints."""

from fastapi.testclient import TestClient

from backend.app.config import settings


def test_get_profile(client: TestClient) -> None:
    resp = client.get("/api/user/profile")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Test User"
    assert data["assistant_name"] == "Clawbolt"
    assert data["onboarding_complete"] is False
    assert data["is_active"] is True
    assert "created_at" in data
    assert "updated_at" in data
    # These fields should no longer be in the response
    assert "trade" not in data
    assert "location" not in data
    assert "hourly_rate" not in data
    assert "business_hours" not in data


def test_profile_defaults_from_settings(client: TestClient) -> None:
    """New user defaults should match the Settings source of truth."""
    resp = client.get("/api/user/profile")
    assert resp.status_code == 200
    data = resp.json()
    assert data["heartbeat_frequency"] == settings.heartbeat_default_frequency
    assert data["preferred_channel"] == settings.messaging_provider


def test_update_profile_partial(client: TestClient) -> None:
    resp = client.put(
        "/api/user/profile",
        json={"name": "Updated Name"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Updated Name"


def test_update_profile_soul_text(client: TestClient) -> None:
    resp = client.put(
        "/api/user/profile",
        json={"soul_text": "Be friendly.", "assistant_name": "Bolt"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["soul_text"] == "Be friendly."
    assert data["assistant_name"] == "Bolt"


def test_update_profile_empty_body(client: TestClient) -> None:
    resp = client.put("/api/user/profile", json={})
    assert resp.status_code == 400
