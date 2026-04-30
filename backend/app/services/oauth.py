"""Generic OAuth 2.0 service with PKCE support.

Handles authorization URL generation, callback processing, token storage,
and automatic token refresh. Tokens are persisted in PostgreSQL (oauth_tokens
table) with encrypted access/refresh token columns.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import sqlalchemy as sa
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from backend.app.config import settings
from backend.app.database import SessionLocal, db_session

logger = logging.getLogger(__name__)


def _refresh_lock_key(user_id: str, integration: str) -> str:
    """Advisory-lock key for serializing OAuth token refresh per (user, integration)."""
    return f"oauth_refresh:{user_id}:{integration}"


# Per-process TTL on cached tokens. A single agent turn loads OAuth
# credentials repeatedly (auth_check during registry build, factory
# create() during tool instantiation, then again per actual tool call).
# In production logs we saw 6+ load_token DB hits per inbound. This
# cache deduplicates them. Kept short so a refresh in another worker
# becomes visible quickly; even a stale read here is safe because the
# returned access_token has its own expires_at that callers honor.
_TOKEN_CACHE_TTL_SECONDS = 30.0

# Shorter TTL for negative results (no token row exists). Cross-worker
# OAuth completion race: worker B handles the OAuth callback and
# save_token invalidates B's local cache, but worker A's cache still
# has a "not connected" entry from a recent is_connected() check. Until
# A's entry expires, A reports the user as unconnected even though
# they just finished connecting. Keep negative TTL short enough that
# the post-OAuth blackout is barely noticeable, but long enough to
# still dedupe the multiple auth_check reads within a single agent turn.
_NEGATIVE_TOKEN_CACHE_TTL_SECONDS = 5.0


# Token expiry buffer: refresh 5 minutes before actual expiry.
_EXPIRY_BUFFER_SECONDS = 300

# Intuit discovery document URL (OpenID Connect configuration).
_INTUIT_DISCOVERY_URL = "https://developer.api.intuit.com/.well-known/openid_configuration"

# Cache TTL for the discovery document (24 hours).
_DISCOVERY_CACHE_TTL_SECONDS = 86400

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


# ---------------------------------------------------------------------------
# Intuit discovery document cache
# ---------------------------------------------------------------------------

_intuit_discovery_cache: dict[str, Any] = {}
_intuit_discovery_fetched_at: float = 0.0


async def warm_intuit_discovery() -> None:
    """Fetch and cache the Intuit OpenID Connect discovery document.

    Called at app startup so that ``get_quickbooks_oauth_config()`` can
    resolve endpoints from the discovery document instead of relying on
    hardcoded URLs. Failures are logged and swallowed; the hardcoded
    fallback URLs will be used until the next successful fetch.
    """
    global _intuit_discovery_cache, _intuit_discovery_fetched_at
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(_INTUIT_DISCOVERY_URL)
            resp.raise_for_status()
            _intuit_discovery_cache = resp.json()
            _intuit_discovery_fetched_at = time.time()
            logger.info(
                "Intuit discovery document cached: authorization_endpoint=%s token_endpoint=%s",
                _intuit_discovery_cache.get("authorization_endpoint"),
                _intuit_discovery_cache.get("token_endpoint"),
            )
    except Exception:
        logger.warning(
            "Failed to fetch Intuit discovery document from %s, "
            "falling back to hardcoded endpoints",
            _INTUIT_DISCOVERY_URL,
            exc_info=True,
        )


def _get_intuit_endpoints() -> tuple[str, str]:
    """Return (authorize_url, token_url) from the discovery cache or fallbacks.

    If the cache is stale (older than ``_DISCOVERY_CACHE_TTL_SECONDS``) or
    missing, falls back to the hardcoded endpoint constants.
    """
    if (
        _intuit_discovery_cache
        and (time.time() - _intuit_discovery_fetched_at) < _DISCOVERY_CACHE_TTL_SECONDS
    ):
        authorize = _intuit_discovery_cache.get(
            "authorization_endpoint", _QBO_AUTHORIZE_URL_FALLBACK
        )
        token = _intuit_discovery_cache.get("token_endpoint", _QBO_TOKEN_URL_FALLBACK)
        return authorize, token
    return _QBO_AUTHORIZE_URL_FALLBACK, _QBO_TOKEN_URL_FALLBACK


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

    State is held in memory (pending authorization flows, a small TTL
    cache of recent token reads) and in PostgreSQL (persisted tokens via
    the oauth_tokens table).

    Token cache semantics:

    - Per-process: each uvicorn worker has its own cache, no shared memory.
    - Positive entries live ``_TOKEN_CACHE_TTL_SECONDS``; negative entries
      live ``_NEGATIVE_TOKEN_CACHE_TTL_SECONDS`` (shorter, to keep the
      post-OAuth-completion blackout small).
    - ``save_token`` and ``delete_token`` invalidate the local cache.
    - Inside an advisory-lock critical section, callers MUST use
      ``_load_token_uncached`` to defeat the cache: the whole point of
      the post-lock reload is to observe a peer worker's just-persisted
      refresh, and a stale cache would mask it.
    - Cross-worker staleness is bounded by the TTL. Within that window a
      worker may return a token whose access_token was rotated by a peer,
      but the cached access_token's own expires_at is honored by callers,
      so behavior is correct.
    """

    def __init__(self) -> None:
        self._pending_states: dict[str, _PendingState] = {}
        self._http: httpx.AsyncClient | None = None
        # In-memory TTL cache for load_token. Keyed by (user_id, integration);
        # value is (token_data_or_none, monotonic_expires_at).
        self._token_cache: dict[tuple[str, str], tuple[OAuthTokenData | None, float]] = {}

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
        # Drop any cached entry so the next load_token re-reads the
        # freshly persisted row (e.g. after a refresh writes new tokens).
        self._token_cache.pop((user_id, integration), None)

    def load_token(
        self,
        user_id: str,
        integration: str,
    ) -> OAuthTokenData | None:
        """Load token data from the oauth_tokens table.

        Results are cached in-memory so a single agent turn (auth_check,
        factory create, tool invocation) does not produce N duplicate DB
        roundtrips. Positive entries cache for ``_TOKEN_CACHE_TTL_SECONDS``;
        negative entries (no row) cache for the shorter
        ``_NEGATIVE_TOKEN_CACHE_TTL_SECONDS`` so a freshly-completed
        OAuth flow on a peer worker becomes visible quickly. The cache
        is invalidated on ``save_token`` and ``delete_token`` within this
        process. Callers inside an advisory-lock critical section must
        use ``_load_token_uncached`` instead.
        """
        cache_key = (user_id, integration)
        now = time.monotonic()
        cached = self._token_cache.get(cache_key)
        if cached is not None and cached[1] > now:
            return cached[0]

        from backend.app.models import OAuthToken

        logger.info(
            "credential.read action=load user=%s integration=%s",
            user_id,
            integration,
        )
        with db_session() as db:
            row = db.execute(
                select(OAuthToken).where(
                    OAuthToken.user_id == user_id,
                    OAuthToken.integration == integration,
                )
            ).scalar_one_or_none()

            if row is None:
                self._token_cache[cache_key] = (
                    None,
                    now + _NEGATIVE_TOKEN_CACHE_TTL_SECONDS,
                )
                return None

            try:
                scopes = json.loads(row.scopes_json) if row.scopes_json else []
            except json.JSONDecodeError:
                scopes = []

            try:
                extra = json.loads(row.extra_json) if row.extra_json else {}
            except json.JSONDecodeError:
                extra = {}

            token = OAuthTokenData(
                access_token=row.access_token,
                refresh_token=row.refresh_token,
                token_type=row.token_type,
                expires_at=row.expires_at,
                scopes=scopes,
                realm_id=row.realm_id,
                extra=extra,
            )
            self._token_cache[cache_key] = (token, now + _TOKEN_CACHE_TTL_SECONDS)
            return token

    def _load_token_uncached(
        self,
        user_id: str,
        integration: str,
    ) -> OAuthTokenData | None:
        """Force a DB read by dropping any cached entry first.

        Use only inside an advisory-lock critical section where the
        whole point of the read is to observe a peer worker's just-
        persisted token. A stale cache would mask the peer's write and
        cause this worker to repeat work (e.g. duplicate HTTP refresh
        that overwrites the rotated refresh_token). All other callers
        should use ``load_token``.
        """
        self._token_cache.pop((user_id, integration), None)
        return self.load_token(user_id, integration)

    def delete_token(
        self,
        user_id: str,
        integration: str,
    ) -> bool:
        """Remove a stored token row."""
        from backend.app.models import OAuthToken

        logger.info(
            "credential.read action=revoke user=%s integration=%s",
            user_id,
            integration,
        )
        with db_session() as db:
            row = db.execute(
                select(OAuthToken).where(
                    OAuthToken.user_id == user_id,
                    OAuthToken.integration == integration,
                )
            ).scalar_one_or_none()

            if row is None:
                self._token_cache.pop((user_id, integration), None)
                return False

            db.delete(row)
            db.commit()
            self._token_cache.pop((user_id, integration), None)
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

    def build_on_refresh_callback(
        self,
        user_id: str,
        integration: str,
    ) -> Callable[[str, str, float], None]:
        """Return a callback that persists tokens refreshed mid-call by a service.

        Provider services (QuickBooks, Google Calendar) refresh on 401 and
        rotate ``refresh_token`` for some providers. Without persisting, the
        rotated refresh token is lost and the next tool call loads the stale
        one from the DB, causing refresh to fail.

        The callback preserves fields the service does not know about
        (realm_id, scopes, extra) by loading the current row before saving.

        The load + save runs under a session-scoped advisory lock keyed on
        ``(user_id, integration)`` so two concurrent service refreshes can't
        both read the old row and overwrite each other (losing the rotated
        refresh_token from whichever callback runs second).
        """

        def _persist(access_token: str, refresh_token: str, expires_at: float) -> None:
            db = SessionLocal()
            lock_key = _refresh_lock_key(user_id, integration)
            try:
                db.execute(
                    text("SELECT pg_advisory_lock(hashtext(:k))"),
                    {"k": lock_key},
                )
                # Close the implicit read transaction; the advisory lock is
                # session-scoped and survives the commit.
                db.commit()
                try:
                    # Bypass the cache: this load is the peer-write detection
                    # point for the on_refresh callback path. Same race as
                    # in refresh_token's post-lock reload.
                    current = self._load_token_uncached(user_id, integration)
                    if current is None:
                        logger.warning(
                            "on_refresh callback fired for missing token: user=%s integration=%s",
                            user_id,
                            integration,
                        )
                        return
                    current.access_token = access_token
                    if refresh_token:
                        current.refresh_token = refresh_token
                    current.expires_at = expires_at
                    self.save_token(user_id, integration, current)
                    logger.info(
                        "Persisted mid-call token refresh: user=%s integration=%s",
                        user_id,
                        integration,
                    )
                finally:
                    try:
                        db.execute(
                            text("SELECT pg_advisory_unlock(hashtext(:k))"),
                            {"k": lock_key},
                        )
                        db.commit()
                    except Exception:
                        logger.exception(
                            "Failed to release OAuth refresh lock in callback: "
                            "user=%s integration=%s",
                            user_id,
                            integration,
                        )
            finally:
                db.close()

        return _persist

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

        A session-scoped Postgres advisory lock serializes concurrent
        refreshes for the same (user, integration). Without it, two workers
        racing a 401 both POST ``refresh_token`` to the provider. Providers
        that rotate the refresh token (Google, QuickBooks) may invalidate
        the other worker's newly-issued token, or the losing worker may
        overwrite the winner's rotated refresh_token in the DB. After
        acquiring the lock we re-load the token and skip the HTTP call
        entirely if another worker already refreshed it.
        """
        logger.info(
            "credential.read action=refresh user=%s integration=%s",
            user_id,
            integration,
        )
        db = SessionLocal()
        lock_key = _refresh_lock_key(user_id, integration)
        try:
            db.execute(
                text("SELECT pg_advisory_lock(hashtext(:k))"),
                {"k": lock_key},
            )
            # Close the implicit read transaction so we aren't idle-in-
            # transaction across the httpx POST. The advisory lock is
            # session-scoped and survives the commit.
            db.commit()
            try:
                # Bypass the cache: the post-lock reload exists to detect
                # a peer worker's just-persisted refresh, which an in-memory
                # cache from before the lock would mask.
                token = self._load_token_uncached(user_id, integration)
                if not token or not token.refresh_token:
                    logger.debug(
                        "Cannot refresh token (missing): user=%s integration=%s "
                        "has_token=%s has_refresh=%s",
                        user_id,
                        integration,
                        token is not None,
                        bool(token and token.refresh_token),
                    )
                    return None

                # If a peer worker already refreshed under the lock, the
                # reloaded token will carry a future expires_at. Skip the
                # redundant HTTP call. ``expires_at > 0`` guards against
                # providers that don't return an expiry (expires_at == 0,
                # treated as non-expiring), where an explicit
                # ``refresh_token`` call still needs to hit the provider.
                if token.expires_at > 0 and not token.is_expired():
                    logger.info(
                        "Token already refreshed by another worker: user=%s integration=%s",
                        user_id,
                        integration,
                    )
                    return token

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
            finally:
                try:
                    db.execute(
                        text("SELECT pg_advisory_unlock(hashtext(:k))"),
                        {"k": lock_key},
                    )
                    db.commit()
                except Exception:
                    logger.exception(
                        "Failed to release OAuth refresh lock: user=%s integration=%s",
                        user_id,
                        integration,
                    )
        finally:
            db.close()

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
# Background refresh scheduler
# ---------------------------------------------------------------------------

# How often the background sweep runs.
_REFRESH_SWEEP_INTERVAL_SECONDS = 120.0

# How far ahead the sweep looks for tokens to refresh. Slightly larger
# than ``_EXPIRY_BUFFER_SECONDS`` (300s) so the sweep catches tokens
# before ``OAuthTokenData.is_expired()`` flips True and the inline
# refresh in ``get_valid_token`` would fire on the user-facing path.
_REFRESH_LOOKAHEAD_SECONDS = 360.0


class OAuthRefreshScheduler:
    """Periodically refresh OAuth tokens before they expire.

    Without this, every user message that arrives during the 5 minute
    pre-expiry window pays the cost of an inline HTTP refresh
    (~150ms). Sweeping in the background pulls that cost off the
    critical path: by the time the user texts, the token is already
    fresh and ``get_valid_token`` returns immediately.

    Failures are logged and swallowed; a single bad token never stops
    the sweep from processing the rest. Inline refresh in
    ``get_valid_token`` remains the safety net for tokens the sweep
    missed (e.g. process just started, sweep hasn't run yet).
    """

    def __init__(self, service: OAuthService) -> None:
        self._service = service
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start the sweep loop (idempotent)."""
        if self._task is not None and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No event loop (sync test harness, etc.): skip silently.
            return
        self._task = loop.create_task(self._run())
        logger.info(
            "OAuth refresh sweep started (interval=%.0fs lookahead=%.0fs)",
            _REFRESH_SWEEP_INTERVAL_SECONDS,
            _REFRESH_LOOKAHEAD_SECONDS,
        )

    def stop(self) -> None:
        """Cancel the sweep loop."""
        if self._task is not None:
            self._task.cancel()
            self._task = None
            logger.info("OAuth refresh sweep stopped")

    async def _run(self) -> None:
        while True:
            try:
                await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("OAuth refresh sweep failed")
            await asyncio.sleep(_REFRESH_SWEEP_INTERVAL_SECONDS)

    async def sweep(self) -> int:
        """Run one sweep. Returns the number of tokens that were refreshed.

        Exposed publicly so tests can drive a single tick without
        spinning up the background task.
        """
        from backend.app.models import OAuthToken

        cutoff = time.time() + _REFRESH_LOOKAHEAD_SECONDS
        with db_session() as db:
            rows = db.execute(
                select(OAuthToken.user_id, OAuthToken.integration)
                .where(OAuthToken.expires_at > 0)
                .where(OAuthToken.expires_at < cutoff)
                .where(OAuthToken.refresh_token != "")
            ).all()
        if not rows:
            return 0
        logger.info("OAuth refresh sweep: %d token(s) due for refresh", len(rows))
        refreshed = 0
        for user_id, integration in rows:
            try:
                result = await self._service.refresh_token(user_id, integration)
            except Exception:
                logger.exception(
                    "Background OAuth refresh failed: user=%s integration=%s",
                    user_id,
                    integration,
                )
                continue
            if result is not None:
                refreshed += 1
        return refreshed


oauth_refresh_scheduler = OAuthRefreshScheduler(oauth_service)


# ---------------------------------------------------------------------------
# Integration-specific config builders
# ---------------------------------------------------------------------------

# QuickBooks OAuth 2.0 endpoint fallbacks (used when the Intuit discovery
# document is unavailable or stale).
_QBO_AUTHORIZE_URL_FALLBACK = "https://appcenter.intuit.com/connect/oauth2"
_QBO_TOKEN_URL_FALLBACK = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
QBO_SCOPES = ["com.intuit.quickbooks.accounting"]

# Google Calendar OAuth 2.0 endpoints
GOOGLE_CALENDAR_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_CALENDAR_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_CALENDAR_SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.readonly",
]

# CompanyCam OAuth 2.0 endpoints
COMPANYCAM_AUTHORIZE_URL = "https://app.companycam.com/oauth/authorize"
COMPANYCAM_TOKEN_URL = "https://app.companycam.com/oauth/token"
COMPANYCAM_SCOPES = ["read", "write", "destroy"]

# Registry of all supported OAuth integrations.
_OAUTH_INTEGRATIONS = ("quickbooks", "google_calendar", "companycam")


def get_quickbooks_oauth_config() -> OAuthConfig | None:
    """Build the QuickBooks OAuth config from settings.

    Endpoint URLs are resolved from the Intuit OpenID Connect discovery
    document when available (populated by ``warm_intuit_discovery()`` at
    startup). Falls back to hardcoded URLs if the discovery document has
    not been fetched or is stale.
    """
    authorize_url, token_url = _get_intuit_endpoints()
    config = OAuthConfig(
        integration="quickbooks",
        client_id=settings.quickbooks_client_id,
        client_secret=settings.quickbooks_client_secret,
        authorize_url=authorize_url,
        token_url=token_url,
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


def get_companycam_oauth_config() -> OAuthConfig | None:
    """Build the CompanyCam OAuth config from settings."""
    config = OAuthConfig(
        integration="companycam",
        client_id=settings.companycam_client_id,
        client_secret=settings.companycam_client_secret,
        authorize_url=COMPANYCAM_AUTHORIZE_URL,
        token_url=COMPANYCAM_TOKEN_URL,
        scopes=COMPANYCAM_SCOPES,
        use_pkce=False,
    )
    return config if config.is_configured else None


def get_oauth_config(integration: str) -> OAuthConfig | None:
    """Return the OAuth config for the named integration, or None."""
    if integration == "quickbooks":
        return get_quickbooks_oauth_config()
    if integration == "google_calendar":
        return get_google_calendar_oauth_config()
    if integration == "companycam":
        return get_companycam_oauth_config()
    return None


def list_oauth_integrations() -> tuple[str, ...]:
    """Return names of all supported OAuth integrations."""
    return _OAUTH_INTEGRATIONS
