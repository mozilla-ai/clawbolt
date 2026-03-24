"""Tests for the OAuth service and router."""

from __future__ import annotations

import time
from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

import backend.app.database as _db_module
from backend.app.agent.file_store import reset_stores
from backend.app.auth.dependencies import get_current_user
from backend.app.config import settings
from backend.app.main import app
from backend.app.models import User
from backend.app.services.oauth import (
    OAuthConfig,
    OAuthService,
    OAuthTokenData,
    _generate_pkce_pair,
    get_google_calendar_oauth_config,
    get_oauth_config,
    get_quickbooks_oauth_config,
    oauth_service,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path) -> Generator[None]:
    with patch.object(settings, "data_dir", str(tmp_path)):
        reset_stores()
        yield
    reset_stores()


@pytest.fixture()
async def test_user() -> User:
    db = _db_module.SessionLocal()
    try:
        user = User(user_id="oauth-test-user", onboarding_complete=True)
        db.add(user)
        db.commit()
        db.refresh(user)
        db.expunge(user)
    finally:
        db.close()
    return user


@pytest.fixture()
def oauth_svc() -> OAuthService:
    """Return a fresh OAuthService (no shared state with the module singleton)."""
    return OAuthService()


@pytest.fixture()
def qb_config() -> OAuthConfig:
    return OAuthConfig(
        integration="quickbooks",
        client_id="test-client-id",
        client_secret="test-client-secret",
        authorize_url="https://appcenter.intuit.com/connect/oauth2",
        token_url="https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        scopes=["com.intuit.quickbooks.accounting"],
    )


@pytest.fixture()
def client(test_user: User) -> Generator[TestClient]:
    def _override() -> User:
        return test_user

    app.dependency_overrides[get_current_user] = _override
    with (
        patch("backend.app.main._verify_llm_settings", new_callable=AsyncMock),
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.start"),
        patch("backend.app.channels.telegram.settings.telegram_allowed_chat_id", "*"),
        patch("backend.app.channels.telegram.settings.telegram_bot_token", ""),
        patch("backend.app.agent.ingestion.settings.message_batch_window_ms", 0),
        TestClient(app) as c,
    ):
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Unit tests: PKCE
# ---------------------------------------------------------------------------


def test_pkce_pair_generation() -> None:
    """PKCE verifier and challenge should be valid base64url strings."""
    verifier, challenge = _generate_pkce_pair()
    assert len(verifier) > 40
    assert len(challenge) > 20
    # Challenge should not contain padding
    assert "=" not in challenge


def test_pkce_pairs_are_unique() -> None:
    """Each call should produce a different verifier/challenge."""
    v1, c1 = _generate_pkce_pair()
    v2, c2 = _generate_pkce_pair()
    assert v1 != v2
    assert c1 != c2


# ---------------------------------------------------------------------------
# Unit tests: OAuthTokenData
# ---------------------------------------------------------------------------


def test_token_data_round_trip() -> None:
    """to_dict/from_dict should preserve all fields."""
    token = OAuthTokenData(
        access_token="at",
        refresh_token="rt",
        token_type="Bearer",
        expires_at=1234567890.0,
        scopes=["scope1"],
        realm_id="realm-123",
        extra={"foo": "bar"},
    )
    restored = OAuthTokenData.from_dict(token.to_dict())
    assert restored.access_token == "at"
    assert restored.refresh_token == "rt"
    assert restored.expires_at == 1234567890.0
    assert restored.scopes == ["scope1"]
    assert restored.realm_id == "realm-123"
    assert restored.extra == {"foo": "bar"}


def test_token_is_expired() -> None:
    """Token with past expires_at should be expired."""
    token = OAuthTokenData(access_token="at", expires_at=time.time() - 100)
    assert token.is_expired() is True


def test_token_not_expired() -> None:
    """Token with future expires_at should not be expired."""
    token = OAuthTokenData(access_token="at", expires_at=time.time() + 3600)
    assert token.is_expired() is False


def test_token_no_expiry_not_expired() -> None:
    """Token with no expiry (0) should never be expired."""
    token = OAuthTokenData(access_token="at", expires_at=0)
    assert token.is_expired() is False


# ---------------------------------------------------------------------------
# Unit tests: token persistence
# ---------------------------------------------------------------------------


def test_save_and_load_token(oauth_svc: OAuthService) -> None:
    """Saved tokens should be loadable."""
    token = OAuthTokenData(
        access_token="at-123",
        refresh_token="rt-456",
        realm_id="realm-1",
        expires_at=time.time() + 3600,
    )
    oauth_svc.save_token("1", "quickbooks", token)
    loaded = oauth_svc.load_token("1", "quickbooks")
    assert loaded is not None
    assert loaded.access_token == "at-123"
    assert loaded.refresh_token == "rt-456"
    assert loaded.realm_id == "realm-1"


def test_load_nonexistent_token(oauth_svc: OAuthService) -> None:
    """Loading a non-existent token should return None."""
    assert oauth_svc.load_token("999", "quickbooks") is None


def test_delete_token(oauth_svc: OAuthService) -> None:
    """Deleting a token should remove the file."""
    token = OAuthTokenData(access_token="at")
    oauth_svc.save_token("1", "quickbooks", token)
    assert oauth_svc.is_connected("1", "quickbooks") is True

    deleted = oauth_svc.delete_token("1", "quickbooks")
    assert deleted is True
    assert oauth_svc.is_connected("1", "quickbooks") is False


def test_delete_nonexistent_token(oauth_svc: OAuthService) -> None:
    """Deleting a non-existent token should return False."""
    assert oauth_svc.delete_token("999", "quickbooks") is False


def test_is_connected(oauth_svc: OAuthService) -> None:
    """is_connected should reflect whether a token file exists."""
    assert oauth_svc.is_connected("1", "quickbooks") is False
    token = OAuthTokenData(access_token="at")
    oauth_svc.save_token("1", "quickbooks", token)
    assert oauth_svc.is_connected("1", "quickbooks") is True


# ---------------------------------------------------------------------------
# Unit tests: authorization URL
# ---------------------------------------------------------------------------


def test_authorization_url_contains_params(oauth_svc: OAuthService, qb_config: OAuthConfig) -> None:
    """Authorization URL should contain client_id, state, PKCE challenge, etc."""
    with patch.object(settings, "app_base_url", "https://myapp.example.com"):
        url = oauth_svc.get_authorization_url(qb_config, user_id="1")

    assert "client_id=test-client-id" in url
    assert "response_type=code" in url
    assert "code_challenge=" in url
    assert "code_challenge_method=S256" in url
    assert "state=" in url
    assert "scope=" in url


def test_authorization_url_uses_app_base_url(
    oauth_svc: OAuthService, qb_config: OAuthConfig
) -> None:
    """The redirect_uri in the URL should use app_base_url."""
    with patch.object(settings, "app_base_url", "https://myapp.example.com"):
        url = oauth_svc.get_authorization_url(qb_config, user_id="1")

    assert "redirect_uri=https%3A%2F%2Fmyapp.example.com%2Fapi%2Foauth%2Fcallback" in url


def test_authorization_url_stores_state(oauth_svc: OAuthService, qb_config: OAuthConfig) -> None:
    """Generating an auth URL should create a pending state entry."""
    url = oauth_svc.get_authorization_url(qb_config, user_id="42")

    # Extract state from URL
    import urllib.parse

    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    state = params["state"][0]

    assert oauth_svc.get_pending_state_integration(state) == "quickbooks"


# ---------------------------------------------------------------------------
# Unit tests: state expiry
# ---------------------------------------------------------------------------


def test_expired_state_returns_none(oauth_svc: OAuthService, qb_config: OAuthConfig) -> None:
    """Expired states should return None for integration lookup."""
    url = oauth_svc.get_authorization_url(qb_config, user_id="1")

    import urllib.parse

    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    state = params["state"][0]

    # Manually expire
    oauth_svc._pending_states[state].expires_at = time.time() - 1

    assert oauth_svc.get_pending_state_integration(state) is None


# ---------------------------------------------------------------------------
# Unit tests: callback handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_handle_callback_invalid_state(oauth_svc: OAuthService) -> None:
    """Callback with unknown state should raise ValueError."""
    with pytest.raises(ValueError, match="Invalid or expired"):
        await oauth_svc.handle_callback("nonexistent", "code123")


@pytest.mark.asyncio()
async def test_handle_callback_exchanges_code(
    oauth_svc: OAuthService, qb_config: OAuthConfig
) -> None:
    """Successful callback should exchange code and store token."""
    url = oauth_svc.get_authorization_url(qb_config, user_id="1")
    import urllib.parse

    state = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["state"][0]

    mock_request = httpx.Request("POST", "https://example.com/token")
    mock_response = httpx.Response(
        200,
        json={
            "access_token": "new-access-token",
            "refresh_token": "new-refresh-token",
            "expires_in": 3600,
            "token_type": "Bearer",
        },
        request=mock_request,
    )

    with patch.object(oauth_svc, "_get_http") as mock_http_fn:
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_http_fn.return_value = mock_client

        with patch(
            "backend.app.services.oauth.get_oauth_config",
            return_value=qb_config,
        ):
            token = await oauth_svc.handle_callback(state, "auth-code", realm_id="realm-1")

    assert token.access_token == "new-access-token"
    assert token.refresh_token == "new-refresh-token"
    assert token.realm_id == "realm-1"

    # Should be persisted
    loaded = oauth_svc.load_token("1", "quickbooks")
    assert loaded is not None
    assert loaded.access_token == "new-access-token"


# ---------------------------------------------------------------------------
# Unit tests: config
# ---------------------------------------------------------------------------


def test_quickbooks_oauth_config_not_configured() -> None:
    """When client_id/secret are empty, config should be None."""
    with (
        patch.object(settings, "quickbooks_client_id", ""),
        patch.object(settings, "quickbooks_client_secret", ""),
    ):
        config = get_quickbooks_oauth_config()
    assert config is None


def test_quickbooks_oauth_config_configured() -> None:
    """When client_id/secret are set, config should be returned."""
    with (
        patch.object(settings, "quickbooks_client_id", "cid"),
        patch.object(settings, "quickbooks_client_secret", "csec"),
    ):
        config = get_quickbooks_oauth_config()
    assert config is not None
    assert config.client_id == "cid"
    assert config.integration == "quickbooks"


def test_google_calendar_oauth_config_not_configured() -> None:
    """When calendar client_id/secret are empty, config should be None."""
    with (
        patch.object(settings, "google_calendar_client_id", ""),
        patch.object(settings, "google_calendar_client_secret", ""),
    ):
        config = get_google_calendar_oauth_config()
    assert config is None


def test_google_calendar_oauth_config_configured() -> None:
    """When calendar client_id/secret are set, config should be returned."""
    with (
        patch.object(settings, "google_calendar_client_id", "gcal-cid"),
        patch.object(settings, "google_calendar_client_secret", "gcal-csec"),
    ):
        config = get_google_calendar_oauth_config()
    assert config is not None
    assert config.client_id == "gcal-cid"
    assert config.integration == "google_calendar"
    assert config.use_pkce is False


def test_get_oauth_config_dispatches_google_calendar() -> None:
    """get_oauth_config('google_calendar') should return Google Calendar config."""
    with (
        patch.object(settings, "google_calendar_client_id", "gcal-cid"),
        patch.object(settings, "google_calendar_client_secret", "gcal-csec"),
    ):
        config = get_oauth_config("google_calendar")
    assert config is not None
    assert config.integration == "google_calendar"


def test_pkce_params_omitted_when_disabled(
    oauth_svc: OAuthService,
) -> None:
    """Authorization URL should not contain PKCE params when use_pkce=False."""
    config = OAuthConfig(
        integration="test_no_pkce",
        client_id="cid",
        client_secret="csec",
        authorize_url="https://example.com/auth",
        token_url="https://example.com/token",
        scopes=["scope1"],
        use_pkce=False,
    )
    url = oauth_svc.get_authorization_url(config, user_id="1")
    assert "code_challenge=" not in url
    assert "code_challenge_method=" not in url


def test_pkce_params_present_when_enabled(
    oauth_svc: OAuthService,
) -> None:
    """Authorization URL should contain PKCE params when use_pkce=True (default)."""
    config = OAuthConfig(
        integration="test_pkce",
        client_id="cid",
        client_secret="csec",
        authorize_url="https://example.com/auth",
        token_url="https://example.com/token",
        scopes=["scope1"],
        use_pkce=True,
    )
    url = oauth_svc.get_authorization_url(config, user_id="1")
    assert "code_challenge=" in url
    assert "code_challenge_method=S256" in url


@pytest.mark.asyncio()
async def test_code_verifier_omitted_when_pkce_disabled(
    oauth_svc: OAuthService,
) -> None:
    """Token exchange should not include code_verifier when use_pkce=False."""
    config = OAuthConfig(
        integration="no_pkce",
        client_id="cid",
        client_secret="csec",
        authorize_url="https://example.com/auth",
        token_url="https://example.com/token",
        scopes=["scope1"],
        use_pkce=False,
    )

    url = oauth_svc.get_authorization_url(config, user_id="1")
    import urllib.parse

    state = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["state"][0]

    mock_request = httpx.Request("POST", "https://example.com/token")
    mock_response = httpx.Response(
        200,
        json={
            "access_token": "at",
            "refresh_token": "rt",
            "expires_in": 3600,
        },
        request=mock_request,
    )

    with patch.object(oauth_svc, "_get_http") as mock_http_fn:
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_http_fn.return_value = mock_client

        with patch(
            "backend.app.services.oauth.get_oauth_config",
            return_value=config,
        ):
            await oauth_svc.handle_callback(state, "auth-code")

    # Verify code_verifier was NOT in the POST data
    call_kwargs = mock_client.post.call_args
    post_data = call_kwargs.kwargs.get("data") or call_kwargs[1].get("data", {})
    assert "code_verifier" not in post_data


# ---------------------------------------------------------------------------
# Router tests
# ---------------------------------------------------------------------------


def test_oauth_status_endpoint(client: TestClient) -> None:
    """GET /api/oauth/status should return integration statuses."""
    with (
        patch.object(settings, "quickbooks_client_id", "cid"),
        patch.object(settings, "quickbooks_client_secret", "csec"),
    ):
        resp = client.get("/api/oauth/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "integrations" in data
    names = [e["integration"] for e in data["integrations"]]
    assert "quickbooks" in names


def test_oauth_authorize_endpoint(client: TestClient) -> None:
    """GET /api/oauth/quickbooks/authorize should return an auth URL."""
    with (
        patch.object(settings, "quickbooks_client_id", "cid"),
        patch.object(settings, "quickbooks_client_secret", "csec"),
        patch.object(settings, "app_base_url", "https://example.com"),
    ):
        resp = client.get("/api/oauth/quickbooks/authorize")
    assert resp.status_code == 200
    data = resp.json()
    assert "url" in data
    assert "appcenter.intuit.com" in data["url"]


def test_oauth_authorize_unconfigured_integration(client: TestClient) -> None:
    """Authorize for an unconfigured integration should return 400."""
    resp = client.get("/api/oauth/nonexistent/authorize")
    assert resp.status_code == 400


def test_oauth_callback_invalid_state(client: TestClient) -> None:
    """Callback with invalid state should redirect with error."""
    resp = client.get(
        "/api/oauth/callback?code=abc&state=invalid",
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "error" in resp.headers["location"]


def test_oauth_disconnect_not_found(client: TestClient) -> None:
    """Disconnecting when not connected should return 404."""
    resp = client.delete("/api/oauth/quickbooks")
    assert resp.status_code == 404


def test_oauth_disconnect_success(client: TestClient, test_user: User) -> None:
    """Disconnecting a connected integration should succeed."""
    # Store a token first
    token = OAuthTokenData(access_token="at")
    oauth_service.save_token(test_user.id, "quickbooks", token)

    resp = client.delete("/api/oauth/quickbooks")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "disconnected"


def test_oauth_callback_with_provider_error(client: TestClient) -> None:
    """Callback with error param should redirect with that error."""
    resp = client.get(
        "/api/oauth/callback?code=&state=&error=access_denied&error_description=User+denied",
        follow_redirects=False,
    )
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert "User" in location and "denied" in location
