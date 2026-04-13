"""Generic OAuth 2.0 service with PKCE support.

Handles authorization URL generation, callback processing, token storage,
and automatic token refresh. Tokens are persisted in PostgreSQL (oauth_tokens
table) with encrypted access/refresh token columns.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from backend.app.config import settings
from backend.app.database import db_session

logger = logging.getLogger(__name__)

# Token expiry buffer: refresh 5 minutes before actual expiry.
_EXPIRY_BUFFER_SECONDS = 300

# OAuth state entries expire after 10 minutes.
_STATE_TTL_SECONDS = 600

# RFC 6749 Section 5.2 error codes indicating a permanently invalid token.
# These mean the user must re-authenticate; retrying will not help.
_PERMANENT_OAUTH_ERROR_CODES = frozenset(
    {
        "invalid_grant",
        "invalid_client",
        "unauthorized_client",
    }
)


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
    source: str = "web"


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


class OAuthService:
    """Manages OAuth flows and token lifecycle.

    State is held in memory (pending authorization flows) and in PostgreSQL
    (persisted tokens via the oauth_tokens table).
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
        source: str = "web",
    ) -> str:
        """Build an authorization URL with PKCE and state parameter.

        *source* tracks where the flow was initiated from ("web" for the
        frontend UI, "chat" for the manage_integration tool). The callback
        uses this to decide whether to redirect to the SPA or render a
        standalone confirmation page.
        """
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
            source=source,
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

    # -- Token persistence (database-backed) ------------------------------------

    def save_token(
        self,
        user_id: str,
        integration: str,
        token: OAuthTokenData,
    ) -> None:
        """Persist token data to the oauth_tokens table (atomic upsert)."""
        from backend.app.models import OAuthToken

        values = {
            "user_id": user_id,
            "integration": integration,
            "access_token": token.access_token,
            "refresh_token": token.refresh_token,
            "token_type": token.token_type,
            "expires_at": token.expires_at,
            "scopes_json": json.dumps(token.scopes),
            "realm_id": token.realm_id,
            "extra_json": json.dumps(token.extra),
        }
        update_cols = {k: v for k, v in values.items() if k not in ("user_id", "integration")}
        update_cols["updated_at"] = sa.func.now()

        stmt = (
            pg_insert(OAuthToken)
            .values(**values)
            .on_conflict_do_update(
                constraint="uq_oauth_token_user_integration",
                set_=update_cols,
            )
        )

        with db_session() as db:
            db.execute(stmt)
            db.commit()

    def load_token(
        self,
        user_id: str,
        integration: str,
    ) -> OAuthTokenData | None:
        """Load token data from the oauth_tokens table."""
        from backend.app.models import OAuthToken

        with db_session() as db:
            row = db.execute(
                select(OAuthToken).where(
                    OAuthToken.user_id == user_id,
                    OAuthToken.integration == integration,
                )
            ).scalar_one_or_none()

            if row is None:
                return None

            try:
                scopes = json.loads(row.scopes_json) if row.scopes_json else []
            except json.JSONDecodeError:
                scopes = []

            try:
                extra = json.loads(row.extra_json) if row.extra_json else {}
            except json.JSONDecodeError:
                extra = {}

            return OAuthTokenData(
                access_token=row.access_token,
                refresh_token=row.refresh_token,
                token_type=row.token_type,
                expires_at=row.expires_at,
                scopes=scopes,
                realm_id=row.realm_id,
                extra=extra,
            )

    def delete_token(
        self,
        user_id: str,
        integration: str,
    ) -> bool:
        """Remove a stored token row."""
        from backend.app.models import OAuthToken

        with db_session() as db:
            row = db.execute(
                select(OAuthToken).where(
                    OAuthToken.user_id == user_id,
                    OAuthToken.integration == integration,
                )
            ).scalar_one_or_none()

            if row is None:
                return False

            db.delete(row)
            db.commit()
            return True

    def is_connected(self, user_id: str, integration: str) -> bool:
        """Check if a valid (non-expired) token exists for this user/integration.

        Returns True when a token row exists and is either not expired or
        has a refresh token that could renew it. Returns False when no
        token exists or the token is expired without a refresh token.
        """
        token = self.load_token(user_id, integration)
        if token is None:
            return False
        if not token.is_expired():
            return True
        # Expired but has a refresh token: still considered "connected"
        # because get_valid_token() will refresh it on next use.
        return bool(token.refresh_token)

    # -- Token refresh with error classification --------------------------------

    @staticmethod
    def _is_permanent_refresh_failure(error: Exception) -> bool:
        """Return True when the refresh error is permanent (user must re-auth).

        Permanent errors (e.g. ``invalid_grant`` from a revoked token) mean
        re-authentication is required. Transient errors (network timeouts,
        provider 5xx) leave the token intact for a later retry.
        """
        if isinstance(error, httpx.HTTPStatusError):
            try:
                body = error.response.json()
                error_code = body.get("error")
                is_permanent = error_code in _PERMANENT_OAUTH_ERROR_CODES
                logger.debug(
                    "OAuth error classification: status=%s error_code=%s permanent=%s body=%s",
                    error.response.status_code,
                    error_code,
                    is_permanent,
                    body,
                )
                return is_permanent
            except Exception:
                logger.debug(
                    "OAuth error response not JSON: status=%s body=%r",
                    error.response.status_code,
                    error.response.text[:200],
                )
                return False
        return False

    async def refresh_token(
        self,
        user_id: str,
        integration: str,
    ) -> OAuthTokenData | None:
        """Refresh an expired OAuth token via the provider's token endpoint.

        Returns the updated token data on success, or None if no token or
        refresh token exists. Raises on HTTP errors so the caller can
        classify them via ``_is_permanent_refresh_failure``.
        """
        token = self.load_token(user_id, integration)
        if not token or not token.refresh_token:
            logger.debug(
                "Cannot refresh token (missing): user=%s integration=%s has_token=%s has_refresh=%s",
                user_id,
                integration,
                token is not None,
                bool(token and token.refresh_token),
            )
            return None

        config = get_oauth_config(integration)
        if config is None:
            logger.debug(
                "Cannot refresh token (no config): user=%s integration=%s",
                user_id,
                integration,
            )
            return None

        logger.debug(
            "Attempting token refresh: user=%s integration=%s token_url=%s",
            user_id,
            integration,
            config.token_url,
        )
        http = self._get_http()
        resp = await http.post(
            config.token_url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": token.refresh_token,
            },
            auth=(config.client_id, config.client_secret),
        )
        resp.raise_for_status()
        data = resp.json()

        token.access_token = data["access_token"]
        if "refresh_token" in data:
            token.refresh_token = data["refresh_token"]

        # Compute absolute expiry from the provider's response.
        # RFC 6749 Section 5.1: expires_in is RECOMMENDED, not REQUIRED.
        # Some providers return an absolute expires_at timestamp instead.
        # When neither is present the token is treated as non-expiring
        # (expires_at stays 0, and is_expired() returns False).
        if data.get("expires_in"):
            token.expires_at = time.time() + int(data["expires_in"])
        elif data.get("expires_at"):
            token.expires_at = float(data["expires_at"])
        else:
            token.expires_at = 0.0

        self.save_token(user_id, integration, token)
        logger.info(
            "Refreshed OAuth token: user=%s integration=%s",
            user_id,
            integration,
        )
        return token

    async def get_valid_token(
        self,
        user_id: str,
        integration: str,
    ) -> OAuthTokenData | None:
        """Return a valid token, refreshing automatically if expired.

        On permanent refresh failure (e.g. revoked grant), deletes the
        stale token and sends a re-auth notification to the user.
        On transient failure (e.g. network error), keeps the token for
        a later retry and returns None.
        """
        token = self.load_token(user_id, integration)
        if not token:
            logger.debug("No token found: user=%s integration=%s", user_id, integration)
            return None

        if not token.is_expired():
            logger.debug(
                "Token valid (not expired): user=%s integration=%s expires_at=%s",
                user_id,
                integration,
                token.expires_at,
            )
            return token

        logger.info(
            "Token expired: user=%s integration=%s expires_at=%s",
            user_id,
            integration,
            token.expires_at,
        )

        if not token.refresh_token:
            logger.warning(
                "Token expired with no refresh token: user=%s integration=%s",
                user_id,
                integration,
            )
            return None

        try:
            return await self.refresh_token(user_id, integration)
        except Exception as exc:
            logger.warning(
                "Token refresh failed: user=%s integration=%s error=%s",
                user_id,
                integration,
                exc,
            )
            if self._is_permanent_refresh_failure(exc):
                logger.warning(
                    "Permanent OAuth failure, deleting token: user=%s integration=%s",
                    user_id,
                    integration,
                )
                self.delete_token(user_id, integration)
                await self._notify_reauth_needed(user_id, integration)
            else:
                logger.info(
                    "Transient OAuth failure, keeping token for retry: user=%s integration=%s",
                    user_id,
                    integration,
                )
            return None

    async def _notify_reauth_needed(
        self,
        user_id: str,
        integration: str,
    ) -> None:
        """Best-effort notification that an OAuth integration has disconnected.

        Looks up the user's active channel route and sends a message via
        the bus. Failures are logged and swallowed so token cleanup is
        never blocked by a notification error.
        """
        try:
            from backend.app.bus import OutboundMessage, message_bus
            from backend.app.models import ChannelRoute

            with db_session() as db:
                route = (
                    db.execute(
                        select(ChannelRoute).where(
                            ChannelRoute.user_id == user_id,
                            ChannelRoute.enabled.is_(True),
                        )
                    )
                    .scalars()
                    .first()
                )

            if route is None:
                logger.debug(
                    "No active channel route for reauth notification: user=%s",
                    user_id,
                )
                return

            friendly = integration.replace("_", " ").title()
            text = (
                f"Your {friendly} connection has expired. "
                "Please reconnect it in Settings > Integrations."
            )

            await message_bus.publish_outbound(
                OutboundMessage(
                    channel=route.channel,
                    chat_id=route.channel_identifier,
                    content=text,
                )
            )
        except Exception:
            logger.warning(
                "Failed to notify user about disconnected integration: user=%s integration=%s",
                user_id,
                integration,
            )

    # -- State management helpers ----------------------------------------------

    def get_pending_state_integration(self, state: str) -> str | None:
        """Return the integration name for a pending state, or None."""
        pending = self._pending_states.get(state)
        if pending is None or time.time() > pending.expires_at:
            return None
        return pending.integration

    def get_pending_state_source(self, state: str) -> str:
        """Return the source ("web" or "chat") for a pending state."""
        pending = self._pending_states.get(state)
        if pending is None or time.time() > pending.expires_at:
            return "web"
        return pending.source

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
