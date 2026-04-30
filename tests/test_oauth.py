"""Tests for the OAuth service and router."""

from __future__ import annotations

import time
from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

import backend.app.database as _db_module
import backend.app.services.oauth as _oauth_module
from backend.app.auth.dependencies import get_current_user
from backend.app.config import settings
from backend.app.main import app
from backend.app.models import User
from backend.app.services.oauth import (
    _DISCOVERY_CACHE_TTL_SECONDS,
    OAuthConfig,
    OAuthService,
    OAuthTokenData,
    _generate_pkce_pair,
    _get_intuit_endpoints,
    get_companycam_oauth_config,
    get_google_calendar_oauth_config,
    get_oauth_config,
    get_quickbooks_oauth_config,
    oauth_service,
    warm_intuit_discovery,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
# Unit tests: token persistence (database-backed)
# ---------------------------------------------------------------------------


def test_save_and_load_token(oauth_svc: OAuthService, test_user: User) -> None:
    """Saved tokens should be loadable from the database."""
    token = OAuthTokenData(
        access_token="at-123",
        refresh_token="rt-456",
        realm_id="realm-1",
        expires_at=time.time() + 3600,
    )
    oauth_svc.save_token(test_user.id, "quickbooks", token)
    loaded = oauth_svc.load_token(test_user.id, "quickbooks")
    assert loaded is not None
    assert loaded.access_token == "at-123"
    assert loaded.refresh_token == "rt-456"
    assert loaded.realm_id == "realm-1"


def test_build_on_refresh_callback_persists_rotated_refresh_token(
    oauth_svc: OAuthService, test_user: User
) -> None:
    """The callback wired into provider services must persist refreshed tokens.

    Regression: QuickBooks/Google Calendar rotate refresh_token on some
    refreshes. If the callback does not save the new value back to the DB,
    subsequent tool calls load the stale refresh_token and re-auth fails.
    """
    original = OAuthTokenData(
        access_token="at-old",
        refresh_token="rt-old",
        realm_id="realm-1",
        scopes=["scope.a", "scope.b"],
        expires_at=time.time() + 3600,
    )
    oauth_svc.save_token(test_user.id, "quickbooks", original)

    callback = oauth_svc.build_on_refresh_callback(test_user.id, "quickbooks")
    new_expires = time.time() + 7200
    callback("at-new", "rt-new", new_expires)

    reloaded = oauth_svc.load_token(test_user.id, "quickbooks")
    assert reloaded is not None
    assert reloaded.access_token == "at-new"
    assert reloaded.refresh_token == "rt-new"
    assert reloaded.realm_id == "realm-1"
    assert reloaded.scopes == ["scope.a", "scope.b"]
    assert abs(reloaded.expires_at - new_expires) < 1


async def test_refresh_token_returns_early_when_peer_worker_already_refreshed(
    oauth_svc: OAuthService, test_user: User
) -> None:
    """When another worker has already refreshed the token while this one
    was waiting for the per-(user, integration) advisory lock, we must
    observe the fresh token under the lock and skip the HTTP refresh call
    entirely. Otherwise both workers POST ``refresh_token`` and the second
    one can either get rejected or overwrite the winner's rotated RT."""
    # Pre-populate with an unexpired token, simulating "peer already refreshed".
    fresh = OAuthTokenData(
        access_token="at-fresh",
        refresh_token="rt-fresh",
        expires_at=time.time() + 7200,
    )
    oauth_svc.save_token(test_user.id, "quickbooks", fresh)

    with patch.object(oauth_svc, "_get_http") as mock_http_fn:
        mock_client = AsyncMock()
        mock_http_fn.return_value = mock_client
        result = await oauth_svc.refresh_token(test_user.id, "quickbooks")

    assert result is not None
    assert result.access_token == "at-fresh"
    mock_client.post.assert_not_called()


async def test_refresh_token_bypasses_cache_for_post_lock_reload(
    oauth_svc: OAuthService, test_user: User
) -> None:
    """The post-advisory-lock reload inside refresh_token must bypass the cache.

    Regression for #1085. Without the bypass, the in-memory token cache
    can hide a peer worker's recently-persisted refresh from this worker:
    we'd return the stale (still-expired) cached token, fall through to
    an unnecessary HTTP refresh, and risk overwriting the rotated
    refresh_token. Simulate the race by priming the cache with an
    expired token, then writing a fresh one straight to the DB to mimic
    a peer worker, and asserting refresh_token observes the fresh value
    without a network call.
    """
    expired = OAuthTokenData(
        access_token="at-stale",
        refresh_token="rt-stale",
        expires_at=time.time() - 100,
    )
    oauth_svc.save_token(test_user.id, "quickbooks", expired)
    # Prime the cache.
    cached = oauth_svc.load_token(test_user.id, "quickbooks")
    assert cached is not None and cached.access_token == "at-stale"

    # Simulate a peer worker writing a freshly-refreshed token directly
    # to the DB (bypassing this OAuthService instance, so our cache stays
    # populated with the expired version).
    fresh_expires = time.time() + 7200
    from sqlalchemy import update

    from backend.app.database import db_session
    from backend.app.models import OAuthToken

    with db_session() as db:
        db.execute(
            update(OAuthToken)
            .where(
                OAuthToken.user_id == test_user.id,
                OAuthToken.integration == "quickbooks",
            )
            .values(access_token="at-peer-fresh", expires_at=fresh_expires)
        )
        db.commit()

    with patch.object(oauth_svc, "_get_http") as mock_http_fn:
        mock_client = AsyncMock()
        mock_http_fn.return_value = mock_client
        result = await oauth_svc.refresh_token(test_user.id, "quickbooks")

    assert result is not None
    assert result.access_token == "at-peer-fresh"
    mock_client.post.assert_not_called()


def test_build_on_refresh_callback_preserves_refresh_token_when_empty(
    oauth_svc: OAuthService, test_user: User
) -> None:
    """When a provider refresh returns no new refresh_token, the callback
    must keep the existing one rather than wiping it."""
    original = OAuthTokenData(
        access_token="at-old",
        refresh_token="rt-original",
        expires_at=time.time() + 3600,
    )
    oauth_svc.save_token(test_user.id, "quickbooks", original)

    callback = oauth_svc.build_on_refresh_callback(test_user.id, "quickbooks")
    callback("at-new", "", time.time() + 7200)

    reloaded = oauth_svc.load_token(test_user.id, "quickbooks")
    assert reloaded is not None
    assert reloaded.access_token == "at-new"
    assert reloaded.refresh_token == "rt-original"


def test_save_token_upsert(oauth_svc: OAuthService, test_user: User) -> None:
    """Saving a token twice should update the existing row, not create a duplicate."""
    token1 = OAuthTokenData(access_token="first")
    oauth_svc.save_token(test_user.id, "quickbooks", token1)

    token2 = OAuthTokenData(access_token="second")
    oauth_svc.save_token(test_user.id, "quickbooks", token2)

    loaded = oauth_svc.load_token(test_user.id, "quickbooks")
    assert loaded is not None
    assert loaded.access_token == "second"


def test_save_token_upsert_updates_timestamp(oauth_svc: OAuthService, test_user: User) -> None:
    """Upserting a token should refresh the updated_at timestamp via sa.func.now()."""
    from sqlalchemy import select, text

    from backend.app.database import db_session
    from backend.app.models import OAuthToken

    token1 = OAuthTokenData(access_token="first")
    oauth_svc.save_token(test_user.id, "quickbooks", token1)

    # Backdate updated_at so the upsert's now() is guaranteed to be later.
    with db_session() as db:
        db.execute(
            text(
                "UPDATE oauth_tokens SET updated_at = updated_at - interval '1 hour'"
                " WHERE user_id = :uid AND integration = :integ"
            ),
            {"uid": test_user.id, "integ": "quickbooks"},
        )
        db.commit()

    with db_session() as db:
        row = db.execute(
            select(OAuthToken).where(
                OAuthToken.user_id == test_user.id,
                OAuthToken.integration == "quickbooks",
            )
        ).scalar_one()
        backdated = row.updated_at

    token2 = OAuthTokenData(access_token="second")
    oauth_svc.save_token(test_user.id, "quickbooks", token2)

    with db_session() as db:
        row = db.execute(
            select(OAuthToken).where(
                OAuthToken.user_id == test_user.id,
                OAuthToken.integration == "quickbooks",
            )
        ).scalar_one()
        assert row.updated_at > backdated


def test_load_nonexistent_token(oauth_svc: OAuthService) -> None:
    """Loading a non-existent token should return None."""
    assert oauth_svc.load_token("999", "quickbooks") is None


def test_delete_token(oauth_svc: OAuthService, test_user: User) -> None:
    """Deleting a token should remove the row."""
    token = OAuthTokenData(access_token="at")
    oauth_svc.save_token(test_user.id, "quickbooks", token)
    assert oauth_svc.is_connected(test_user.id, "quickbooks") is True

    deleted = oauth_svc.delete_token(test_user.id, "quickbooks")
    assert deleted is True
    assert oauth_svc.is_connected(test_user.id, "quickbooks") is False


def test_delete_nonexistent_token(oauth_svc: OAuthService) -> None:
    """Deleting a non-existent token should return False."""
    assert oauth_svc.delete_token("999", "quickbooks") is False


def test_load_token_cached_within_ttl(oauth_svc: OAuthService, test_user: User) -> None:
    """A second load_token within the TTL window should not hit the database.

    Regression for #1085. A single agent turn loads OAuth credentials
    multiple times (auth_check, factory create, tool invocation). Without
    a cache, that produced 6+ duplicate DB roundtrips per inbound message
    in production logs.
    """
    token = OAuthTokenData(access_token="at-cached", expires_at=time.time() + 3600)
    oauth_svc.save_token(test_user.id, "quickbooks", token)

    # Prime the cache.
    first = oauth_svc.load_token(test_user.id, "quickbooks")
    assert first is not None and first.access_token == "at-cached"

    # Out-of-band delete the row so a fresh DB read would return None.
    # If the cache works, the second load_token should still return
    # the cached value.
    from sqlalchemy import delete

    from backend.app.database import db_session
    from backend.app.models import OAuthToken

    with db_session() as db:
        db.execute(
            delete(OAuthToken).where(
                OAuthToken.user_id == test_user.id,
                OAuthToken.integration == "quickbooks",
            )
        )
        db.commit()

    second = oauth_svc.load_token(test_user.id, "quickbooks")
    assert second is not None
    assert second.access_token == "at-cached"


def test_save_token_invalidates_cache(oauth_svc: OAuthService, test_user: User) -> None:
    """save_token must drop the cache entry so the next load sees the new value."""
    initial = OAuthTokenData(access_token="at-initial", expires_at=time.time() + 3600)
    oauth_svc.save_token(test_user.id, "quickbooks", initial)
    primed = oauth_svc.load_token(test_user.id, "quickbooks")
    assert primed is not None and primed.access_token == "at-initial"

    rotated = OAuthTokenData(access_token="at-rotated", expires_at=time.time() + 3600)
    oauth_svc.save_token(test_user.id, "quickbooks", rotated)

    reloaded = oauth_svc.load_token(test_user.id, "quickbooks")
    assert reloaded is not None
    assert reloaded.access_token == "at-rotated"


def test_delete_token_invalidates_cache(oauth_svc: OAuthService, test_user: User) -> None:
    """delete_token must drop the cache entry so the next load returns None."""
    token = OAuthTokenData(access_token="at", expires_at=time.time() + 3600)
    oauth_svc.save_token(test_user.id, "quickbooks", token)
    primed = oauth_svc.load_token(test_user.id, "quickbooks")
    assert primed is not None

    oauth_svc.delete_token(test_user.id, "quickbooks")
    assert oauth_svc.load_token(test_user.id, "quickbooks") is None


def test_load_token_caches_negative_lookup(oauth_svc: OAuthService, test_user: User) -> None:
    """A None result is also cached so repeated 'not connected' checks
    do not flood the DB.

    Real-world trigger: the agent runs auth_check on every specialist
    factory at registry build time. For users who have not connected
    QuickBooks, that is N negative DB reads per inbound. Cache both
    positive and negative results.
    """
    # First load: row does not exist, returns None.
    assert oauth_svc.load_token(test_user.id, "quickbooks") is None

    # Out-of-band insert a row.
    token = OAuthTokenData(access_token="at-now-exists", expires_at=time.time() + 3600)
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from backend.app.database import db_session
    from backend.app.models import OAuthToken

    with db_session() as db:
        db.execute(
            pg_insert(OAuthToken).values(
                user_id=test_user.id,
                integration="quickbooks",
                access_token=token.access_token,
                refresh_token="",
                token_type="Bearer",
                expires_at=token.expires_at,
                scopes_json="[]",
                realm_id="",
                extra_json="{}",
            )
        )
        db.commit()

    # Within the TTL the cached None should still be returned.
    assert oauth_svc.load_token(test_user.id, "quickbooks") is None


def test_is_connected(oauth_svc: OAuthService, test_user: User) -> None:
    """is_connected should reflect whether a token row exists."""
    assert oauth_svc.is_connected(test_user.id, "quickbooks") is False
    token = OAuthTokenData(access_token="at")
    oauth_svc.save_token(test_user.id, "quickbooks", token)
    assert oauth_svc.is_connected(test_user.id, "quickbooks") is True


def test_scopes_and_extra_round_trip(oauth_svc: OAuthService, test_user: User) -> None:
    """Scopes and extra dict should survive save/load via JSON serialization."""
    token = OAuthTokenData(
        access_token="at",
        scopes=["scope1", "scope2"],
        extra={"key": "value"},
    )
    oauth_svc.save_token(test_user.id, "quickbooks", token)
    loaded = oauth_svc.load_token(test_user.id, "quickbooks")
    assert loaded is not None
    assert loaded.scopes == ["scope1", "scope2"]
    assert loaded.extra == {"key": "value"}


def test_multiple_integrations_per_user(oauth_svc: OAuthService, test_user: User) -> None:
    """Different integrations for the same user should be independent."""
    oauth_svc.save_token(test_user.id, "quickbooks", OAuthTokenData(access_token="qb-token"))
    oauth_svc.save_token(test_user.id, "google_calendar", OAuthTokenData(access_token="gcal-token"))

    qb = oauth_svc.load_token(test_user.id, "quickbooks")
    gcal = oauth_svc.load_token(test_user.id, "google_calendar")
    assert qb is not None and qb.access_token == "qb-token"
    assert gcal is not None and gcal.access_token == "gcal-token"


# ---------------------------------------------------------------------------
# Unit tests: encryption round-trip
# ---------------------------------------------------------------------------


def test_encrypted_token_round_trip(oauth_svc: OAuthService, test_user: User) -> None:
    """Tokens should survive save/load with encryption enabled."""
    with patch.object(settings, "encryption_key", SecretStr("test-key-at-least-16-chars!!")):
        token = OAuthTokenData(
            access_token="secret-access",
            refresh_token="secret-refresh",
        )
        oauth_svc.save_token(test_user.id, "quickbooks", token)
        loaded = oauth_svc.load_token(test_user.id, "quickbooks")

    assert loaded is not None
    assert loaded.access_token == "secret-access"
    assert loaded.refresh_token == "secret-refresh"


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
    oauth_svc: OAuthService, qb_config: OAuthConfig, test_user: User
) -> None:
    """Successful callback should exchange code and store token."""
    url = oauth_svc.get_authorization_url(qb_config, user_id=test_user.id)
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

    # Should be persisted in DB
    loaded = oauth_svc.load_token(test_user.id, "quickbooks")
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
    assert config.extra_auth_params == {"access_type": "offline", "prompt": "consent"}


def test_google_calendar_auth_url_includes_access_type_offline(
    oauth_svc: OAuthService,
) -> None:
    """Google Calendar auth URL must include access_type=offline for refresh tokens."""
    with (
        patch.object(settings, "google_calendar_client_id", "gcal-cid"),
        patch.object(settings, "google_calendar_client_secret", "gcal-csec"),
    ):
        config = get_google_calendar_oauth_config()
    assert config is not None
    url = oauth_svc.get_authorization_url(config, user_id="1")
    assert "access_type=offline" in url
    assert "prompt=consent" in url


def test_companycam_oauth_config_not_configured() -> None:
    """When companycam client_id/secret are empty, config should be None."""
    with (
        patch.object(settings, "companycam_client_id", ""),
        patch.object(settings, "companycam_client_secret", ""),
    ):
        config = get_companycam_oauth_config()
    assert config is None


def test_companycam_oauth_config_configured() -> None:
    """When companycam client_id/secret are set, config should be returned."""
    with (
        patch.object(settings, "companycam_client_id", "cc-cid"),
        patch.object(settings, "companycam_client_secret", "cc-csec"),
    ):
        config = get_companycam_oauth_config()
    assert config is not None
    assert config.client_id == "cc-cid"
    assert config.integration == "companycam"
    assert config.use_pkce is False
    assert config.scopes == ["read", "write", "destroy"]


def test_get_oauth_config_dispatches_companycam() -> None:
    """get_oauth_config('companycam') should return CompanyCam config."""
    with (
        patch.object(settings, "companycam_client_id", "cc-cid"),
        patch.object(settings, "companycam_client_secret", "cc-csec"),
    ):
        config = get_oauth_config("companycam")
    assert config is not None
    assert config.integration == "companycam"


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
    oauth_svc: OAuthService, test_user: User
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

    url = oauth_svc.get_authorization_url(config, user_id=test_user.id)
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
    assert "companycam" in names


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


def test_oauth_callback_missing_code(client: TestClient) -> None:
    """Callback without code param (e.g. user denied) should redirect with error."""
    resp = client.get(
        "/api/oauth/callback?error=access_denied",
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "error" in resp.headers["location"]


def test_oauth_callback_empty_code_no_error(client: TestClient) -> None:
    """Callback with empty code and no error should redirect with error."""
    resp = client.get(
        "/api/oauth/callback?state=some_state",
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "error" in resp.headers["location"]


# ---------------------------------------------------------------------------
# Chat-initiated OAuth callback (standalone HTML)
# ---------------------------------------------------------------------------


def test_chat_callback_success_returns_html(
    client: TestClient, qb_config: OAuthConfig, test_user: User
) -> None:
    """Chat-initiated OAuth success should return HTML, not a redirect."""
    url = oauth_service.get_authorization_url(qb_config, user_id=test_user.id, source="chat")
    import urllib.parse as _up

    state = _up.parse_qs(_up.urlparse(url).query)["state"][0]

    mock_request = httpx.Request("POST", "https://example.com/token")
    mock_response = httpx.Response(
        200,
        json={
            "access_token": "at",
            "refresh_token": "rt",
            "expires_in": 3600,
            "token_type": "Bearer",
        },
        request=mock_request,
    )
    with (
        patch.object(oauth_service, "_get_http") as mock_http_fn,
        patch(
            "backend.app.services.oauth.get_oauth_config",
            return_value=qb_config,
        ),
    ):
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_http_fn.return_value = mock_client

        resp = client.get(
            f"/api/oauth/callback?code=auth-code&state={state}&realmId=r1",
            follow_redirects=False,
        )

    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Connected" in resp.text
    assert "close this tab" in resp.text


def test_chat_callback_error_returns_html(client: TestClient) -> None:
    """Chat-initiated OAuth error should return HTML, not a redirect."""
    # Inject a pending state with source="chat" so the callback renders HTML
    import time

    from backend.app.services.oauth import _PendingState

    state_key = "chat-error-test-state"
    oauth_service._pending_states[state_key] = _PendingState(
        user_id="test",
        integration="google_calendar",
        code_verifier="v",
        redirect_uri="http://localhost/api/oauth/callback",
        expires_at=time.time() + 600,
        source="chat",
    )

    resp = client.get(
        f"/api/oauth/callback?state={state_key}&error=access_denied"
        "&error_description=User+denied+access",
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Connection Failed" in resp.text
    assert "User denied access" in resp.text


def test_web_callback_still_redirects(
    client: TestClient, qb_config: OAuthConfig, test_user: User
) -> None:
    """Web-initiated OAuth success should still redirect to the SPA."""
    url = oauth_service.get_authorization_url(qb_config, user_id=test_user.id)
    import urllib.parse as _up

    state = _up.parse_qs(_up.urlparse(url).query)["state"][0]

    mock_request = httpx.Request("POST", "https://example.com/token")
    mock_response = httpx.Response(
        200,
        json={
            "access_token": "at",
            "refresh_token": "rt",
            "expires_in": 3600,
            "token_type": "Bearer",
        },
        request=mock_request,
    )
    with (
        patch.object(oauth_service, "_get_http") as mock_http_fn,
        patch(
            "backend.app.services.oauth.get_oauth_config",
            return_value=qb_config,
        ),
    ):
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_http_fn.return_value = mock_client

        resp = client.get(
            f"/api/oauth/callback?code=auth-code&state={state}&realmId=r1",
            follow_redirects=False,
        )

    assert resp.status_code == 302
    assert "/app/oauth/callback?status=success" in resp.headers["location"]


# ---------------------------------------------------------------------------
# Intuit discovery document tests
# ---------------------------------------------------------------------------

_FAKE_DISCOVERY = {
    "authorization_endpoint": "https://discovered.intuit.com/oauth2/authorize",
    "token_endpoint": "https://discovered.intuit.com/oauth2/token",
    "userinfo_endpoint": "https://discovered.intuit.com/userinfo",
    "issuer": "https://oauth.platform.intuit.com/op/v1",
}


@pytest.fixture(autouse=False)
def _reset_discovery_cache() -> Generator[None]:
    """Reset the module-level discovery cache before and after each test."""
    _oauth_module._intuit_discovery_cache.clear()
    _oauth_module._intuit_discovery_fetched_at = 0.0
    yield
    _oauth_module._intuit_discovery_cache.clear()
    _oauth_module._intuit_discovery_fetched_at = 0.0


@pytest.mark.asyncio()
async def test_warm_intuit_discovery_success(_reset_discovery_cache: None) -> None:
    """Successful discovery fetch should cache endpoints."""
    mock_resp = httpx.Response(200, json=_FAKE_DISCOVERY, request=httpx.Request("GET", "https://x"))
    with patch("backend.app.services.oauth.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        await warm_intuit_discovery()

    assert _oauth_module._intuit_discovery_cache["authorization_endpoint"] == (
        "https://discovered.intuit.com/oauth2/authorize"
    )
    assert _oauth_module._intuit_discovery_cache["token_endpoint"] == (
        "https://discovered.intuit.com/oauth2/token"
    )


@pytest.mark.asyncio()
async def test_warm_intuit_discovery_failure_swallowed(_reset_discovery_cache: None) -> None:
    """Failed discovery fetch should not raise; cache stays empty."""
    with patch("backend.app.services.oauth.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.get.side_effect = httpx.ConnectError("network error")
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        await warm_intuit_discovery()

    assert _oauth_module._intuit_discovery_cache == {}


def test_get_intuit_endpoints_from_cache(_reset_discovery_cache: None) -> None:
    """When the discovery cache is warm, endpoints come from it."""
    _oauth_module._intuit_discovery_cache.update(_FAKE_DISCOVERY)
    _oauth_module._intuit_discovery_fetched_at = time.time()

    authorize, token = _get_intuit_endpoints()
    assert authorize == "https://discovered.intuit.com/oauth2/authorize"
    assert token == "https://discovered.intuit.com/oauth2/token"


def test_get_intuit_endpoints_fallback_when_empty(_reset_discovery_cache: None) -> None:
    """When the discovery cache is empty, hardcoded fallbacks are returned."""
    authorize, token = _get_intuit_endpoints()
    assert authorize == "https://appcenter.intuit.com/connect/oauth2"
    assert token == "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"


def test_get_intuit_endpoints_fallback_when_stale(_reset_discovery_cache: None) -> None:
    """When the cache is older than the TTL, fallbacks are returned."""
    _oauth_module._intuit_discovery_cache.update(_FAKE_DISCOVERY)
    _oauth_module._intuit_discovery_fetched_at = time.time() - _DISCOVERY_CACHE_TTL_SECONDS - 1

    authorize, token = _get_intuit_endpoints()
    assert authorize == "https://appcenter.intuit.com/connect/oauth2"
    assert token == "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"


def test_quickbooks_config_uses_discovery_endpoints(_reset_discovery_cache: None) -> None:
    """get_quickbooks_oauth_config should use discovery endpoints when cached."""
    _oauth_module._intuit_discovery_cache.update(_FAKE_DISCOVERY)
    _oauth_module._intuit_discovery_fetched_at = time.time()

    with (
        patch.object(settings, "quickbooks_client_id", "cid"),
        patch.object(settings, "quickbooks_client_secret", "csec"),
    ):
        config = get_quickbooks_oauth_config()

    assert config is not None
    assert config.authorize_url == "https://discovered.intuit.com/oauth2/authorize"
    assert config.token_url == "https://discovered.intuit.com/oauth2/token"
