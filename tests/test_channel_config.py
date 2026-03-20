"""Tests for channel config GET/PUT endpoints."""

import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from backend.app.config import settings


@pytest.fixture()
def _set_bot_token() -> Iterator[None]:
    """Ensure settings has a known bot token for tests that need it."""
    original = settings.telegram_bot_token
    settings.telegram_bot_token = "test-token-123"
    yield
    settings.telegram_bot_token = original


@pytest.fixture()
def _clear_bot_token() -> Iterator[None]:
    """Ensure settings has no bot token."""
    original = settings.telegram_bot_token
    settings.telegram_bot_token = ""
    yield
    settings.telegram_bot_token = original


def test_get_channel_config_token_set(client: TestClient, _set_bot_token: None) -> None:
    """GET returns telegram_bot_token_set=True when token is configured."""
    resp = client.get("/api/user/channels/config")
    assert resp.status_code == 200
    data = resp.json()
    assert data["telegram_bot_token_set"] is True
    # Should never leak the actual token
    assert "test-token-123" not in str(data)


def test_get_channel_config_token_not_set(client: TestClient, _clear_bot_token: None) -> None:
    """GET returns telegram_bot_token_set=False when token is empty."""
    resp = client.get("/api/user/channels/config")
    assert resp.status_code == 200
    data = resp.json()
    assert data["telegram_bot_token_set"] is False


def test_update_channel_config_token(client: TestClient, _clear_bot_token: None) -> None:
    """PUT with a new token updates settings in-memory and GET reflects change."""
    resp = client.put(
        "/api/user/channels/config",
        json={"telegram_bot_token": "new-bot-token-456"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["telegram_bot_token_set"] is True

    # Verify settings updated in-memory
    assert settings.telegram_bot_token == "new-bot-token-456"

    # GET should also reflect the change
    resp2 = client.get("/api/user/channels/config")
    assert resp2.json()["telegram_bot_token_set"] is True

    # Clean up
    settings.telegram_bot_token = ""


def test_update_channel_config_persists_to_config_json(
    client: TestClient, tmp_path: Path, _clear_bot_token: None
) -> None:
    """PUT with a token writes to config.json in the data directory."""
    config_path = tmp_path / "config.json"

    with patch(
        "backend.app.routers.user_profile.save_persistent_config",
        wraps=lambda updates, path=None: _write_config(config_path, updates),
    ):
        resp = client.put(
            "/api/user/channels/config",
            json={"telegram_bot_token": "persisted-token"},
        )

    assert resp.status_code == 200
    config_data = json.loads(config_path.read_text())
    assert config_data["telegram_bot_token"] == "persisted-token"

    # Clean up
    settings.telegram_bot_token = ""


def _write_config(path: Path, updates: dict[str, str]) -> None:
    """Helper to write config.json for testing."""
    existing: dict[str, str] = {}
    if path.is_file():
        existing = json.loads(path.read_text())
    existing.update(updates)
    path.write_text(json.dumps(existing, indent=2) + "\n")


def test_update_channel_config_null_token_is_ignored(
    client: TestClient, _set_bot_token: None
) -> None:
    """PUT with null token should be ignored, preserving the existing value."""
    original_ids = settings.telegram_allowed_chat_ids
    with patch("backend.app.routers.user_profile.save_persistent_config"):
        resp = client.put(
            "/api/user/channels/config",
            json={"telegram_bot_token": None, "telegram_allowed_chat_ids": "111"},
        )
    assert resp.status_code == 200
    assert resp.json()["telegram_bot_token_set"] is True
    assert settings.telegram_bot_token == "test-token-123"
    settings.telegram_allowed_chat_ids = original_ids


def test_update_channel_config_null_only_returns_400(
    client: TestClient, _set_bot_token: None
) -> None:
    """PUT with only null fields should return 400 (no effective updates)."""
    resp = client.put(
        "/api/user/channels/config",
        json={"telegram_bot_token": None},
    )
    assert resp.status_code == 400
    assert settings.telegram_bot_token == "test-token-123"


def test_update_channel_config_empty_string_clears_token(
    client: TestClient, _set_bot_token: None
) -> None:
    """PUT with empty string explicitly clears the token."""
    resp = client.put(
        "/api/user/channels/config",
        json={"telegram_bot_token": ""},
    )
    assert resp.status_code == 200
    assert resp.json()["telegram_bot_token_set"] is False
    assert settings.telegram_bot_token == ""


def test_get_channel_config_includes_allowed_chat_ids(
    client: TestClient,
) -> None:
    """GET response includes telegram_allowed_chat_ids field."""
    original = settings.telegram_allowed_chat_ids
    settings.telegram_allowed_chat_ids = "111,222,333"
    try:
        resp = client.get("/api/user/channels/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["telegram_allowed_chat_ids"] == "111,222,333"
    finally:
        settings.telegram_allowed_chat_ids = original


def test_update_channel_config_allowed_chat_ids(
    client: TestClient,
) -> None:
    """PUT updates telegram_allowed_chat_ids in settings."""
    original = settings.telegram_allowed_chat_ids
    with patch("backend.app.routers.user_profile.save_persistent_config"):
        try:
            resp = client.put(
                "/api/user/channels/config",
                json={"telegram_allowed_chat_ids": "444,555"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["telegram_allowed_chat_ids"] == "444,555"
            assert settings.telegram_allowed_chat_ids == "444,555"
        finally:
            settings.telegram_allowed_chat_ids = original
