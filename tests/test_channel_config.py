"""Tests for channel config GET/PUT endpoints."""

from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

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


@pytest.fixture()
def _stub_store() -> Iterator[MagicMock]:
    """Replace the SettingsStore with a no-op mock for tests that PUT.

    The route tests care about HTTP semantics and in-memory ``settings``
    mutation, not actual persistence. The mock captures ``save`` calls
    so individual tests can assert on them when relevant.
    """
    store = MagicMock()
    store.save = AsyncMock()
    store.delete = AsyncMock()
    store.load = AsyncMock(return_value={})
    with patch(
        "backend.app.routers.user_profile.get_settings_store",
        return_value=store,
    ):
        yield store


def test_get_channel_config_token_set(client: TestClient, _set_bot_token: None) -> None:
    """GET returns telegram_bot_token_set=True when token is configured."""
    resp = client.get("/api/user/channels/config")
    assert resp.status_code == 200
    data = resp.json()
    assert data["telegram_bot_token_set"] is True
    # Should never leak the actual token.
    assert "test-token-123" not in str(data)


def test_get_channel_config_token_not_set(client: TestClient, _clear_bot_token: None) -> None:
    """GET returns telegram_bot_token_set=False when token is empty."""
    resp = client.get("/api/user/channels/config")
    assert resp.status_code == 200
    data = resp.json()
    assert data["telegram_bot_token_set"] is False


def test_update_channel_config_token(
    client: TestClient, _clear_bot_token: None, _stub_store: MagicMock
) -> None:
    """PUT with a new token updates settings in-memory and GET reflects change."""
    resp = client.put(
        "/api/user/channels/config",
        json={"telegram_bot_token": "new-bot-token-456"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["telegram_bot_token_set"] is True
    assert settings.telegram_bot_token == "new-bot-token-456"

    # GET should also reflect the change.
    resp2 = client.get("/api/user/channels/config")
    assert resp2.json()["telegram_bot_token_set"] is True

    settings.telegram_bot_token = ""


def test_update_channel_config_persists_via_store(
    client: TestClient, _clear_bot_token: None, _stub_store: MagicMock
) -> None:
    """PUT routes the update through the SettingsStore."""
    resp = client.put(
        "/api/user/channels/config",
        json={"telegram_bot_token": "persisted-token"},
    )

    assert resp.status_code == 200
    _stub_store.save.assert_called_once()
    saved_updates, save_kwargs = _stub_store.save.call_args
    assert saved_updates[0] == {"telegram_bot_token": "persisted-token"}
    assert "actor_user_id" in save_kwargs

    settings.telegram_bot_token = ""


def test_update_channel_config_strips_mask_round_trip(
    client: TestClient, _set_bot_token: None, _stub_store: MagicMock
) -> None:
    """PUT carrying ``********`` for a secret is treated as 'no change'."""
    resp = client.put(
        "/api/user/channels/config",
        json={"telegram_bot_token": "********", "telegram_allowed_chat_id": "111"},
    )
    assert resp.status_code == 200
    # The token is unchanged in-memory.
    assert settings.telegram_bot_token == "test-token-123"
    # Only the non-secret field went to the store.
    saved_updates, _ = _stub_store.save.call_args
    assert saved_updates[0] == {"telegram_allowed_chat_id": "111"}


def test_update_channel_config_null_token_is_ignored(
    client: TestClient, _set_bot_token: None, _stub_store: MagicMock
) -> None:
    """PUT with null token should be ignored, preserving the existing value."""
    original_id = settings.telegram_allowed_chat_id
    resp = client.put(
        "/api/user/channels/config",
        json={"telegram_bot_token": None, "telegram_allowed_chat_id": "111"},
    )
    assert resp.status_code == 200
    assert resp.json()["telegram_bot_token_set"] is True
    assert settings.telegram_bot_token == "test-token-123"
    settings.telegram_allowed_chat_id = original_id


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
    client: TestClient, _set_bot_token: None, _stub_store: MagicMock
) -> None:
    """PUT with empty string explicitly clears the token."""
    resp = client.put(
        "/api/user/channels/config",
        json={"telegram_bot_token": ""},
    )
    assert resp.status_code == 200
    assert resp.json()["telegram_bot_token_set"] is False
    assert settings.telegram_bot_token == ""


def test_get_channel_config_includes_allowed_chat_id(
    client: TestClient,
) -> None:
    """GET response includes telegram_allowed_chat_id field."""
    original = settings.telegram_allowed_chat_id
    settings.telegram_allowed_chat_id = "111222333"
    try:
        resp = client.get("/api/user/channels/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["telegram_allowed_chat_id"] == "111222333"
    finally:
        settings.telegram_allowed_chat_id = original


def test_update_channel_config_allowed_chat_id(client: TestClient, _stub_store: MagicMock) -> None:
    """PUT updates telegram_allowed_chat_id in settings."""
    original = settings.telegram_allowed_chat_id
    try:
        resp = client.put(
            "/api/user/channels/config",
            json={"telegram_allowed_chat_id": "444555"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["telegram_allowed_chat_id"] == "444555"
        assert settings.telegram_allowed_chat_id == "444555"
    finally:
        settings.telegram_allowed_chat_id = original


def test_update_channel_config_rejects_multiple_chat_ids(
    client: TestClient, _stub_store: MagicMock
) -> None:
    """PUT rejects comma-separated chat IDs (only a single ID is allowed)."""
    resp = client.put(
        "/api/user/channels/config",
        json={"telegram_allowed_chat_id": "111,222"},
    )
    assert resp.status_code == 422
    assert "single" in resp.json()["detail"].lower()


@pytest.fixture()
def _imessage_reset() -> Iterator[None]:
    """Reset iMessage backend settings to empty around a test."""
    original_linq = settings.linq_api_token
    original_bb_url = settings.bluebubbles_server_url
    original_bb_pw = settings.bluebubbles_password
    settings.linq_api_token = ""
    settings.bluebubbles_server_url = ""
    settings.bluebubbles_password = ""
    yield
    settings.linq_api_token = original_linq
    settings.bluebubbles_server_url = original_bb_url
    settings.bluebubbles_password = original_bb_pw


def test_channel_config_imessage_backend_none(client: TestClient, _imessage_reset: None) -> None:
    """imessage_backend is None when neither Linq nor BlueBubbles is configured."""
    resp = client.get("/api/user/channels/config")
    assert resp.status_code == 200
    assert resp.json()["imessage_backend"] is None


def test_channel_config_imessage_backend_linq(client: TestClient, _imessage_reset: None) -> None:
    """imessage_backend reports 'linq' when only Linq is configured."""
    settings.linq_api_token = "tok"
    resp = client.get("/api/user/channels/config")
    assert resp.status_code == 200
    assert resp.json()["imessage_backend"] == "linq"


def test_channel_config_imessage_backend_bluebubbles(
    client: TestClient, _imessage_reset: None
) -> None:
    """imessage_backend reports 'bluebubbles' when only BlueBubbles is configured."""
    settings.bluebubbles_server_url = "https://mac.ngrok.io"
    settings.bluebubbles_password = "p"
    resp = client.get("/api/user/channels/config")
    assert resp.status_code == 200
    assert resp.json()["imessage_backend"] == "bluebubbles"
