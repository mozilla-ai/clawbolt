"""Tests for user profile endpoints."""

from fastapi.testclient import TestClient

from backend.app.config import settings


def test_get_profile(client: TestClient) -> None:
    resp = client.get("/api/user/profile")
    assert resp.status_code == 200
    data = resp.json()
    assert data["onboarding_complete"] is True
    assert data["is_active"] is True
    assert "created_at" in data
    assert "updated_at" in data
    # These fields should not be in the response
    assert "name" not in data
    assert "assistant_name" not in data
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
        json={"phone": "+15559999999"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["phone"] == "+15559999999"


def test_update_profile_soul_text(client: TestClient) -> None:
    resp = client.put(
        "/api/user/profile",
        json={"soul_text": "Be friendly."},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["soul_text"] == "Be friendly."


def test_update_profile_empty_body(client: TestClient) -> None:
    resp = client.put("/api/user/profile", json={})
    assert resp.status_code == 400


def test_get_profile_includes_llm_fields(client: TestClient) -> None:
    """Profile response should include llm_model and llm_provider."""
    resp = client.get("/api/user/profile")
    assert resp.status_code == 200
    data = resp.json()
    assert "llm_model" in data
    assert "llm_provider" in data
    # Defaults are empty strings (use server defaults)
    assert data["llm_model"] == ""
    assert data["llm_provider"] == ""


def test_update_profile_llm_model_and_provider(client: TestClient) -> None:
    """Users should be able to set their preferred model and provider."""
    resp = client.put(
        "/api/user/profile",
        json={"llm_model": "gpt-4o", "llm_provider": "openai"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["llm_model"] == "gpt-4o"
    assert data["llm_provider"] == "openai"


def test_update_profile_clear_llm_model(client: TestClient) -> None:
    """Setting llm_model to empty string should revert to server default."""
    # Set a model first
    client.put("/api/user/profile", json={"llm_model": "gpt-4o"})
    # Then clear it
    resp = client.put("/api/user/profile", json={"llm_model": ""})
    assert resp.status_code == 200
    assert resp.json()["llm_model"] == ""
