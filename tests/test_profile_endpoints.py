"""Tests for user profile endpoints."""

from unittest.mock import AsyncMock, patch

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


def test_update_profile_onboarding_complete(client: TestClient) -> None:
    """PUT /api/user/profile can set onboarding_complete flag."""
    resp = client.put(
        "/api/user/profile",
        json={"onboarding_complete": True},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["onboarding_complete"] is True


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
    assert "vision_provider" in data
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


# ---------------------------------------------------------------------------
# Provider / model listing
# ---------------------------------------------------------------------------


def test_list_providers(client: TestClient) -> None:
    """GET /user/providers returns the any-llm provider list."""
    resp = client.get("/api/user/providers")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) > 0
    names = [p["name"] for p in data]
    assert "anthropic" in names
    assert "openai" in names
    # Hidden meta-providers should not appear
    assert "platform" not in names
    assert "gateway" not in names


def test_provider_has_local_flag(client: TestClient) -> None:
    """Providers include a local flag distinguishing local vs cloud."""
    resp = client.get("/api/user/providers")
    data = resp.json()
    by_name = {p["name"]: p for p in data}
    assert by_name["anthropic"]["local"] is False
    assert by_name["openai"]["local"] is False
    assert by_name["ollama"]["local"] is True


@patch(
    "backend.app.routers.user_profile.get_models",
    new_callable=AsyncMock,
)
def test_list_provider_models(mock_get_models: AsyncMock, client: TestClient) -> None:
    """GET /user/providers/{provider}/models returns model list."""
    mock_get_models.return_value = ["claude-sonnet-4-20250514", "claude-haiku-4-5-20251001"]
    resp = client.get("/api/user/providers/anthropic/models")
    assert resp.status_code == 200
    data = resp.json()
    assert "claude-sonnet-4-20250514" in data
    assert "claude-haiku-4-5-20251001" in data


@patch(
    "backend.app.routers.user_profile.get_models",
    new_callable=AsyncMock,
)
def test_list_provider_models_error_returns_502(
    mock_get_models: AsyncMock, client: TestClient
) -> None:
    """GET /user/providers/{provider}/models returns 502 on failure."""
    mock_get_models.side_effect = RuntimeError("Connection refused")
    resp = client.get("/api/user/providers/badprovider/models")
    assert resp.status_code == 502
    assert "Failed to list models" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Storage config
# ---------------------------------------------------------------------------


def test_get_storage_config(client: TestClient) -> None:
    """GET /user/storage/config returns current server-level storage settings."""
    resp = client.get("/api/user/storage/config")
    assert resp.status_code == 200
    data = resp.json()
    assert "storage_provider" in data
    assert "file_storage_base_dir" in data
    assert isinstance(data["dropbox_access_token_set"], bool)
    assert isinstance(data["google_drive_credentials_json_set"], bool)


def test_update_storage_config_provider(client: TestClient) -> None:
    """PUT /user/storage/config updates the storage provider."""
    original = settings.storage_provider
    try:
        resp = client.put(
            "/api/user/storage/config",
            json={"storage_provider": "dropbox"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["storage_provider"] == "dropbox"
        assert settings.storage_provider == "dropbox"
    finally:
        settings.storage_provider = original


def test_update_storage_config_credentials(client: TestClient) -> None:
    """PUT /user/storage/config with a token sets the _set flag to True."""
    original = settings.dropbox_access_token
    try:
        resp = client.put(
            "/api/user/storage/config",
            json={"dropbox_access_token": "test-token-123"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["dropbox_access_token_set"] is True
    finally:
        settings.dropbox_access_token = original


def test_update_storage_config_invalid_provider(client: TestClient) -> None:
    """PUT /user/storage/config rejects unknown providers."""
    resp = client.put(
        "/api/user/storage/config",
        json={"storage_provider": "s3"},
    )
    assert resp.status_code == 422


def test_update_storage_config_empty_body(client: TestClient) -> None:
    """PUT /user/storage/config with empty body returns 400."""
    resp = client.put("/api/user/storage/config", json={})
    assert resp.status_code == 400
