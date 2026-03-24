"""Generic OAuth 2.0 service with PKCE support.

Handles authorization URL generation, callback processing, token storage,
and automatic token refresh. Tokens are stored as JSON files in the user's
data directory, consistent with Clawbolt's file-based storage pattern.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from backend.app.config import settings

logger = logging.getLogger(__name__)

# Token expiry buffer: refresh 5 minutes before actual expiry.
_EXPIRY_BUFFER_SECONDS = 300

# OAuth state entries expire after 10 minutes.
_STATE_TTL_SECONDS = 600


@dataclass
class OAuthConfig:
    """Configuration for an OAuth 2.0 integration."""

    integration: str
    client_id: str
    client_secret: str
    authorize_url: str
    token_url: str
    scopes: list[str]
    callback_path: str = "/api/oauth/callback"
    use_pkce: bool = True
    extra_auth_params: dict[str, str] = field(default_factory=dict)

    @property
    def is_configured(self) -> bool:
        return bool(self.client_id and self.client_secret)


@dataclass
class _PendingState:
    """In-memory record for a pending OAuth authorization."""

    user_id: str
    integration: str
    code_verifier: str
    redirect_uri: str
    expires_at: float


@dataclass
class OAuthTokenData:
    """Stored OAuth token data."""

    access_token: str
    refresh_token: str = ""
    token_type: str = "Bearer"
    expires_at: float = 0.0
    scopes: list[str] = field(default_factory=list)
    realm_id: str = ""  # QuickBooks company ID
    extra: dict[str, Any] = field(default_factory=dict)

    def is_expired(self) -> bool:
        if self.expires_at <= 0:
            return False
        return time.time() >= (self.expires_at - _EXPIRY_BUFFER_SECONDS)

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_type": self.token_type,
            "expires_at": self.expires_at,
            "scopes": self.scopes,
            "realm_id": self.realm_id,
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OAuthTokenData:
        return cls(
            access_token=data.get("access_token", ""),
            refresh_token=data.get("refresh_token", ""),
            token_type=data.get("token_type", "Bearer"),
            expires_at=data.get("expires_at", 0.0),
            scopes=data.get("scopes", []),
            realm_id=data.get("realm_id", ""),
            extra=data.get("extra", {}),
        )


def _generate_pkce_pair() -> tuple[str, str]:
    """Generate a PKCE code_verifier and code_challenge (S256)."""
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _oauth_token_path(user_id: str, integration: str) -> Path:
    """Return the file path for storing an OAuth token."""
    return Path(settings.data_dir) / str(user_id) / "oauth" / f"{integration}.json"


class OAuthService:
    """Manages OAuth flows and token lifecycle.

    State is held in memory (pending authorization flows) and on disk
    (persisted tokens). This matches Clawbolt's single-process, file-based
    storage architecture.
    """

    def __init__(self) -> None:
        self._pending_states: dict[str, _PendingState] = {}
        self._http: httpx.AsyncClient | None = None

    def _get_http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=30.0)
        return self._http

    # -- Authorization URL generation ------------------------------------------

    def get_authorization_url(
        self,
        config: OAuthConfig,
        user_id: str,
    ) -> str:
        """Build an authorization URL with PKCE and state parameter."""
        self._cleanup_expired_states()

        state = secrets.token_urlsafe(32)
        verifier, challenge = _generate_pkce_pair()

        base_url = settings.app_base_url.rstrip("/")
        redirect_uri = f"{base_url}{config.callback_path}"

        self._pending_states[state] = _PendingState(
            user_id=user_id,
            integration=config.integration,
            code_verifier=verifier,
            redirect_uri=redirect_uri,
            expires_at=time.time() + _STATE_TTL_SECONDS,
        )

        params: dict[str, str] = {
            "client_id": config.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(config.scopes),
            "state": state,
        }
        if config.use_pkce:
            params["code_challenge"] = challenge
            params["code_challenge_method"] = "S256"
        if config.extra_auth_params:
            params.update(config.extra_auth_params)

        return str(httpx.URL(config.authorize_url, params=params))

    # -- Callback handling -----------------------------------------------------

    async def handle_callback(
        self,
        state: str,
        code: str,
        *,
        realm_id: str = "",
    ) -> OAuthTokenData:
        """Exchange an authorization code for tokens and store them."""
        pending = self._pending_states.pop(state, None)
        if pending is None:
            raise ValueError("Invalid or expired OAuth state")

        if time.time() > pending.expires_at:
            raise ValueError("OAuth state has expired")

        config = get_oauth_config(pending.integration)
        if config is None:
            raise ValueError(f"No OAuth config for integration: {pending.integration}")

        token_data = await self._exchange_code(
            config=config,
            code=code,
            redirect_uri=pending.redirect_uri,
            code_verifier=pending.code_verifier,
        )

        if realm_id:
            token_data.realm_id = realm_id

        self.save_token(pending.user_id, pending.integration, token_data)
        return token_data

    async def _exchange_code(
        self,
        config: OAuthConfig,
        code: str,
        redirect_uri: str,
        code_verifier: str,
    ) -> OAuthTokenData:
        """Exchange authorization code for access and refresh tokens."""
        http = self._get_http()
        token_data: dict[str, str] = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        }
        if config.use_pkce:
            token_data["code_verifier"] = code_verifier
        resp = await http.post(
            config.token_url,
            data=token_data,
            auth=(config.client_id, config.client_secret),
        )
        resp.raise_for_status()
        body = resp.json()

        expires_at = 0.0
        if "expires_in" in body:
            expires_at = time.time() + body["expires_in"]

        scope_raw = body.get("scope", "")
        scopes = scope_raw.split() if isinstance(scope_raw, str) else []

        return OAuthTokenData(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token", ""),
            token_type=body.get("token_type", "Bearer"),
            expires_at=expires_at,
            scopes=scopes,
        )

    # -- Token persistence (file-based) ----------------------------------------

    def save_token(
        self,
        user_id: str,
        integration: str,
        token: OAuthTokenData,
    ) -> None:
        """Persist token data to the user's data directory."""
        path = _oauth_token_path(user_id, integration)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(token.to_dict(), indent=2) + "\n", encoding="utf-8")

    def load_token(
        self,
        user_id: str,
        integration: str,
    ) -> OAuthTokenData | None:
        """Load token data from the user's data directory."""
        path = _oauth_token_path(user_id, integration)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return OAuthTokenData.from_dict(data)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load OAuth token for %s: %s", integration, exc)
            return None

    def delete_token(
        self,
        user_id: str,
        integration: str,
    ) -> bool:
        """Remove stored token data."""
        path = _oauth_token_path(user_id, integration)
        if path.is_file():
            path.unlink()
            return True
        return False

    def is_connected(self, user_id: str, integration: str) -> bool:
        """Check if a token file exists (no refresh check)."""
        return _oauth_token_path(user_id, integration).is_file()

    # -- State management helpers ----------------------------------------------

    def get_pending_state_integration(self, state: str) -> str | None:
        """Return the integration name for a pending state, or None."""
        pending = self._pending_states.get(state)
        if pending is None or time.time() > pending.expires_at:
            return None
        return pending.integration

    def _cleanup_expired_states(self) -> None:
        """Remove expired pending states."""
        now = time.time()
        expired = [k for k, v in self._pending_states.items() if now > v.expires_at]
        for k in expired:
            del self._pending_states[k]


# Module-level singleton.
oauth_service = OAuthService()


# ---------------------------------------------------------------------------
# Integration-specific config builders
# ---------------------------------------------------------------------------

# QuickBooks OAuth 2.0 endpoints
QBO_AUTHORIZE_URL = "https://appcenter.intuit.com/connect/oauth2"
QBO_TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
QBO_SCOPES = ["com.intuit.quickbooks.accounting"]

# Google Calendar OAuth 2.0 endpoints
GOOGLE_CALENDAR_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_CALENDAR_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_CALENDAR_SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.readonly",
]

# Registry of all supported OAuth integrations.
_OAUTH_INTEGRATIONS = ("quickbooks", "google_calendar")


def get_quickbooks_oauth_config() -> OAuthConfig | None:
    """Build the QuickBooks OAuth config from settings."""
    config = OAuthConfig(
        integration="quickbooks",
        client_id=settings.quickbooks_client_id,
        client_secret=settings.quickbooks_client_secret,
        authorize_url=QBO_AUTHORIZE_URL,
        token_url=QBO_TOKEN_URL,
        scopes=QBO_SCOPES,
    )
    return config if config.is_configured else None


def get_google_calendar_oauth_config() -> OAuthConfig | None:
    """Build the Google Calendar OAuth config from settings."""
    config = OAuthConfig(
        integration="google_calendar",
        client_id=settings.google_calendar_client_id,
        client_secret=settings.google_calendar_client_secret,
        authorize_url=GOOGLE_CALENDAR_AUTHORIZE_URL,
        token_url=GOOGLE_CALENDAR_TOKEN_URL,
        scopes=GOOGLE_CALENDAR_SCOPES,
        use_pkce=False,
        extra_auth_params={"access_type": "offline", "prompt": "consent"},
    )
    return config if config.is_configured else None


def get_oauth_config(integration: str) -> OAuthConfig | None:
    """Return the OAuth config for the named integration, or None."""
    if integration == "quickbooks":
        return get_quickbooks_oauth_config()
    if integration == "google_calendar":
        return get_google_calendar_oauth_config()
    return None


def list_oauth_integrations() -> tuple[str, ...]:
    """Return names of all supported OAuth integrations."""
    return _OAUTH_INTEGRATIONS
