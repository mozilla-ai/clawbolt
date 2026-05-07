"""Tests for user profile endpoints."""

import datetime as _dt
from unittest.mock import AsyncMock, patch

import pytest
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


def test_update_profile_ignores_onboarding_complete(client: TestClient) -> None:
    """PUT /api/user/profile cannot set onboarding_complete.

    The flag is backend-owned (flipped by OnboardingSubscriber when the
    LLM deletes BOOTSTRAP.md) so the UI can't short-circuit the
    conversational onboarding by toggling it directly.
    """
    # Sending only onboarding_complete = empty body after field stripping.
    resp = client.put(
        "/api/user/profile",
        json={"onboarding_complete": False},
    )
    assert resp.status_code == 400

    # The flag stays at its fixture default (True) even when bundled with
    # a legitimate field: onboarding_complete is silently dropped.
    resp = client.put(
        "/api/user/profile",
        json={"onboarding_complete": False, "phone": "+15551111111"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["phone"] == "+15551111111"
    assert data["onboarding_complete"] is True


def test_update_profile_empty_body(client: TestClient) -> None:
    resp = client.put("/api/user/profile", json={})
    assert resp.status_code == 400


def test_get_profile_includes_heartbeat_max_daily(client: TestClient) -> None:
    """GET /api/user/profile returns heartbeat_max_daily field."""
    resp = client.get("/api/user/profile")
    assert resp.status_code == 200
    data = resp.json()
    assert "heartbeat_max_daily" in data
    assert data["heartbeat_max_daily"] == 0


def test_update_profile_heartbeat_max_daily(client: TestClient) -> None:
    """PUT /api/user/profile can set heartbeat_max_daily."""
    resp = client.put(
        "/api/user/profile",
        json={"heartbeat_max_daily": 10},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["heartbeat_max_daily"] == 10


# ---------------------------------------------------------------------------
# Data sharing consent
# ---------------------------------------------------------------------------


def test_get_profile_includes_data_sharing_consent_defaults(client: TestClient) -> None:
    """GET /api/user/profile exposes the consent state. Default is opt-out."""
    resp = client.get("/api/user/profile")
    assert resp.status_code == 200
    data = resp.json()
    assert data["data_sharing_consent"] is False
    assert data["data_sharing_consent_at"] is None


def test_get_data_sharing_consent_default(client: TestClient) -> None:
    """Dedicated GET endpoint returns the same state as the profile blob."""
    resp = client.get("/api/user/data-sharing-consent")
    assert resp.status_code == 200
    data = resp.json()
    assert data == {"data_sharing_consent": False, "data_sharing_consent_at": None}


def test_opt_in_stamps_consent_timestamp(client: TestClient) -> None:
    """Opting in flips the bool AND records when it happened."""
    resp = client.put("/api/user/data-sharing-consent", json={"consent": True})
    assert resp.status_code == 200
    data = resp.json()
    assert data["data_sharing_consent"] is True
    assert data["data_sharing_consent_at"] is not None
    # ISO-8601 with timezone (datetime.isoformat() on a tz-aware datetime).
    assert "T" in data["data_sharing_consent_at"]


def test_opt_out_also_stamps_consent_timestamp(client: TestClient) -> None:
    """Opting OUT also bumps the timestamp.

    The column tracks "last toggled" not "first opted in"; without this,
    a user who opts in and later opts out would still appear (by
    timestamp) as having an active recent consent.
    """
    # First opt in.
    r1 = client.put("/api/user/data-sharing-consent", json={"consent": True})
    assert r1.status_code == 200
    t1 = r1.json()["data_sharing_consent_at"]
    assert t1 is not None

    # Then opt out: timestamp must update to a >= value (allow equality
    # because the test runs faster than datetime resolution).
    r2 = client.put("/api/user/data-sharing-consent", json={"consent": False})
    assert r2.status_code == 200
    data = r2.json()
    assert data["data_sharing_consent"] is False
    assert data["data_sharing_consent_at"] is not None
    assert data["data_sharing_consent_at"] >= t1


def test_data_sharing_consent_not_writable_via_generic_profile_put(client: TestClient) -> None:
    """``data_sharing_consent`` must not be settable via PUT /user/profile.

    The dedicated endpoint always stamps the timestamp; the generic
    patch endpoint doesn't, so accepting it there would silently bypass
    the timestamp guarantee.
    """
    resp = client.put(
        "/api/user/profile",
        json={"data_sharing_consent": True, "phone": "+15552222222"},
    )
    # Other field still applies; the consent field is silently dropped
    # because Pydantic with extra='ignore' (default) strips unknowns.
    assert resp.status_code == 200
    data = resp.json()
    assert data["phone"] == "+15552222222"
    assert data["data_sharing_consent"] is False
    assert data["data_sharing_consent_at"] is None


def test_consent_timestamp_uses_overridable_clock(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The consent setter pulls "now" from a single helper so tests can
    pin the clock to a known instant. Without this seam, asserting that
    a specific PUT produced a specific timestamp depends on real
    wall-clock time, which is flaky inside a millisecond.
    """
    fixed = _dt.datetime(2026, 5, 1, 12, 0, 0, tzinfo=_dt.UTC)
    monkeypatch.setattr(
        "backend.app.routers.user_profile._data_sharing_consent_now",
        lambda: fixed,
    )

    resp = client.put("/api/user/data-sharing-consent", json={"consent": True})
    assert resp.status_code == 200
    assert resp.json()["data_sharing_consent_at"] == fixed.isoformat()


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
