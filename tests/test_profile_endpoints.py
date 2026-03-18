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


def test_get_model_config(client: TestClient) -> None:
    """GET /user/model/config returns current server-level LLM settings."""
    resp = client.get("/api/user/model/config")
    assert resp.status_code == 200
    data = resp.json()
    assert "llm_model" in data
    assert "llm_provider" in data
    assert "vision_model" in data
    assert "heartbeat_model" in data
    assert "heartbeat_provider" in data
    assert "compaction_model" in data
    assert "compaction_provider" in data
    assert "llm_api_base" in data


def test_update_model_config(client: TestClient) -> None:
    """PUT /user/model/config updates server-level LLM settings."""
    original_model = settings.llm_model
    original_provider = settings.llm_provider
    try:
        resp = client.put(
            "/api/user/model/config",
            json={"llm_model": "gpt-4o", "llm_provider": "openai"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["llm_model"] == "gpt-4o"
        assert data["llm_provider"] == "openai"
        assert settings.llm_model == "gpt-4o"
        assert settings.llm_provider == "openai"
    finally:
        settings.llm_model = original_model
        settings.llm_provider = original_provider


def test_update_model_config_vision(client: TestClient) -> None:
    """PUT /user/model/config can set task-specific model overrides."""
    original = settings.vision_model
    try:
        resp = client.put(
            "/api/user/model/config",
            json={"vision_model": "gpt-4o-mini"},
        )
        assert resp.status_code == 200
        assert resp.json()["vision_model"] == "gpt-4o-mini"
        assert settings.vision_model == "gpt-4o-mini"
    finally:
        settings.vision_model = original


def test_update_model_config_empty_body(client: TestClient) -> None:
    resp = client.put("/api/user/model/config", json={})
    assert resp.status_code == 400
