"""ServiceTitan credential storage and client-credentials token minting.

ServiceTitan exposes an OAuth 2.0 ``client_credentials`` grant rather than
a user-consent flow: each tenant gives the integrator a Client ID, a
Client Secret, and a Tenant ID, and the integrator separately holds an
app-level App Key that goes in the ``ST-App-Key`` header on every call.

This module persists those four values in the ``oauth_tokens`` row for
``integration='servicetitan'``:

* ``access_token`` (encrypted) carries the current bearer token. Empty
  before the first mint, repopulated on every refresh.
* ``expires_at`` carries the absolute Unix timestamp at which the bearer
  expires. The real ServiceTitan token lives 15 minutes; we honor the
  ``expires_in`` returned by the token endpoint.
* ``extra_json`` carries the tenant-specific metadata:
  ``{tenant_id, client_id, client_secret, app_key}``. ``client_secret``
  is kept here rather than in a dedicated encrypted column because
  ``oauth_tokens.refresh_token`` is reserved for OAuth refresh tokens
  and the surrounding code paths (e.g. ``oauth_service.refresh_token``)
  assume the standard ``grant_type=refresh_token`` shape. Storing the
  secret in ``extra_json`` keeps the row shape compatible with the
  existing OAuth machinery without overloading semantics. The
  ``extra_json`` column is plaintext on the wire to the DB, but the
  row-level access patterns (``oauth_service.load_token`` only) keep
  the value inside the same trust boundary as the encrypted columns;
  if that ever changes, switching to ``refresh_token`` is a one-line
  edit.

The connect tool calls :func:`save_credentials` with the user's pasted
values plus the freshly minted access token; downstream tools call
:func:`get_valid_token` to lazily refresh the bearer on each call.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from backend.app.config import settings
from backend.app.integrations.servicetitan._fake import build_fake_transport
from backend.app.services.oauth import OAuthTokenData, oauth_service

logger = logging.getLogger(__name__)

INTEGRATION_NAME = "servicetitan"
"""Value stored in ``oauth_tokens.integration`` for this integration."""


# Path on ``settings.servicetitan_api_base_url`` that mints client-credentials
# bearer tokens. The real ServiceTitan endpoint and the in-process fake both
# expose ``POST /connect/token``.
TOKEN_PATH = "/connect/token"

# Treat a token as expired this many seconds before its declared expiry so
# the bearer never lapses mid-call. The real bearer lives 15 minutes; a
# 30-second buffer is short enough to keep token mints rare and long enough
# to absorb clock skew plus any in-flight request.
_TOKEN_EXPIRY_BUFFER_SECONDS = 30

# Per-request timeout for the token endpoint. The mint itself is fast; the
# generous ceiling absorbs cold-path TLS handshakes against real ServiceTitan.
_TOKEN_REQUEST_TIMEOUT_SECONDS = 30.0


class ServiceTitanAuthError(RuntimeError):
    """Raised when token minting against ServiceTitan fails.

    Surfaces to the user via the connect tool when the pasted credentials
    are wrong, and via the refresh path when an existing credential stops
    working (e.g. the tenant rotated the secret).
    """


@dataclass
class ServiceTitanCredential:
    """Loaded ServiceTitan credential for a single tenant.

    The fields mirror what gets persisted: the four user-supplied values
    plus the integrator's app-level App Key (snapshotted at connect time
    so a later operator-level App Key rotation does not silently break
    existing tenants until they reconnect).
    """

    tenant_id: str
    client_id: str
    client_secret: str
    app_key: str
    access_token: str = ""
    expires_at: float = 0.0

    def is_token_expired(self, *, now: float | None = None) -> bool:
        """Return True when the stored bearer is empty or about to expire."""
        if not self.access_token:
            return True
        if self.expires_at <= 0:
            # No expiry recorded yet. Treat as expired so the first call
            # mints a fresh token rather than presenting an unknown one.
            return True
        current = now if now is not None else time.time()
        return current >= (self.expires_at - _TOKEN_EXPIRY_BUFFER_SECONDS)


def _build_token_client() -> httpx.AsyncClient:
    """Return an httpx client wired to the configured ServiceTitan host.

    When ``settings.servicetitan_use_fake`` is true the client routes
    through the in-process fake (no real network). Otherwise it points
    at ``settings.servicetitan_api_base_url`` directly. Same code path
    in both modes; the only difference is the transport.
    """
    timeout = httpx.Timeout(_TOKEN_REQUEST_TIMEOUT_SECONDS)
    if settings.servicetitan_use_fake:
        return httpx.AsyncClient(
            transport=build_fake_transport(),
            base_url=settings.servicetitan_api_base_url,
            timeout=timeout,
        )
    return httpx.AsyncClient(
        base_url=settings.servicetitan_api_base_url,
        timeout=timeout,
    )


async def mint_access_token(
    *,
    client_id: str,
    client_secret: str,
) -> tuple[str, float]:
    """Exchange a tenant's client credentials for a Bearer access token.

    Returns ``(access_token, expires_at)`` where ``expires_at`` is an
    absolute Unix timestamp. Raises :class:`ServiceTitanAuthError` for
    any non-2xx response or transport failure.

    The credentials are scoped to one tenant; the App Key header is not
    required for ``/connect/token`` (only resource endpoints check it).
    """
    body = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }
    try:
        async with _build_token_client() as client:
            resp = await client.post(TOKEN_PATH, data=body)
    except httpx.HTTPError as exc:
        logger.warning("ServiceTitan token mint network failure: %s", exc)
        raise ServiceTitanAuthError(f"ServiceTitan token endpoint unreachable: {exc!r}") from exc

    if resp.status_code >= 400:
        logger.warning(
            "ServiceTitan token mint failed: status=%d body=%s",
            resp.status_code,
            resp.text[:500],
        )
        raise ServiceTitanAuthError(
            f"ServiceTitan rejected the client credentials (HTTP {resp.status_code})"
        )

    try:
        payload: dict[str, Any] = resp.json()
    except ValueError as exc:
        raise ServiceTitanAuthError("ServiceTitan token endpoint returned non-JSON body") from exc

    access_token = payload.get("access_token") or ""
    if not access_token:
        raise ServiceTitanAuthError("ServiceTitan token endpoint returned no access_token")

    expires_in = payload.get("expires_in")
    if isinstance(expires_in, int | float) and expires_in > 0:
        expires_at = time.time() + float(expires_in)
    else:
        # The fake always populates ``expires_in``; the real API is
        # documented to as well. Fall back to a short ceiling rather
        # than 0 so a missing field does not produce a token that the
        # cache treats as immortal.
        expires_at = time.time() + 60.0
    return access_token, expires_at


def _serialize_extra(cred: ServiceTitanCredential) -> dict[str, Any]:
    """Build the ``extra_json`` payload from a credential."""
    return {
        "tenant_id": cred.tenant_id,
        "client_id": cred.client_id,
        "client_secret": cred.client_secret,
        "app_key": cred.app_key,
    }


def _credential_from_extra(extra: dict[str, Any], token: OAuthTokenData) -> ServiceTitanCredential:
    """Reconstruct a credential from a loaded token row's extra_json."""
    return ServiceTitanCredential(
        tenant_id=str(extra.get("tenant_id") or ""),
        client_id=str(extra.get("client_id") or ""),
        client_secret=str(extra.get("client_secret") or ""),
        app_key=str(extra.get("app_key") or ""),
        access_token=token.access_token,
        expires_at=token.expires_at,
    )


async def save_credentials(
    user_id: str,
    *,
    tenant_id: str,
    client_id: str,
    client_secret: str,
    app_key: str,
    access_token: str = "",
    expires_at: float = 0.0,
) -> ServiceTitanCredential:
    """Persist (or replace) the ServiceTitan credential for a user.

    Returns the credential object that was written so callers can pass it
    straight to a service constructor without re-reading.
    """
    cred = ServiceTitanCredential(
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret=client_secret,
        app_key=app_key,
        access_token=access_token,
        expires_at=expires_at,
    )
    token = OAuthTokenData(
        access_token=access_token,
        refresh_token="",
        token_type="Bearer",
        expires_at=expires_at,
        scopes=[],
        realm_id=tenant_id,
        extra=_serialize_extra(cred),
    )
    await oauth_service.save_token(user_id, INTEGRATION_NAME, token)
    logger.info(
        "ServiceTitan credentials saved: user=%s tenant=%s",
        user_id,
        tenant_id,
    )
    return cred


async def load_credentials(user_id: str) -> ServiceTitanCredential | None:
    """Load the user's persisted ServiceTitan credential, if any."""
    token = await oauth_service.load_token(user_id, INTEGRATION_NAME)
    if token is None:
        return None
    extra = token.extra or {}
    cred = _credential_from_extra(extra, token)
    if not cred.tenant_id or not cred.client_id or not cred.client_secret:
        # Partial row (e.g. a failed connect that wrote nothing useful).
        # Surface as "not connected" rather than handing the caller a
        # half-built credential.
        return None
    return cred


async def clear_credentials(user_id: str) -> None:
    """Delete the stored ServiceTitan credential. Used on disconnect."""
    await oauth_service.delete_token(user_id, INTEGRATION_NAME)


async def is_connected(user_id: str) -> bool:
    """Return True when a usable ServiceTitan credential is on file.

    Cheap auth-check used by ``auth_check`` callbacks: confirms the row
    has tenant + client credentials but does not exercise the token
    endpoint. The first real tool call refreshes the bearer if needed.
    """
    cred = await load_credentials(user_id)
    return cred is not None


async def get_valid_token(
    user_id: str, *, force_refresh: bool = False
) -> ServiceTitanCredential | None:
    """Return a credential with a current bearer, minting one if needed.

    The wrapper around :func:`load_credentials` plus :func:`mint_access_token`
    that the service layer calls per request. Persists the freshly minted
    bearer back to ``oauth_tokens`` so peer workers and subsequent calls
    in this process see it via the OAuth service cache.

    Set ``force_refresh=True`` to mint a new bearer even when the stored
    expiry is in the future. Used by the service's 401 retry path: the
    real ServiceTitan API can revoke a bearer before its declared
    ``expires_in`` (e.g. on a secret rotation), and the recorded
    expires_at would otherwise mask the revocation.

    Returns ``None`` when no credential exists; raises
    :class:`ServiceTitanAuthError` when minting against the token endpoint
    fails (caller decides whether to clear the row).
    """
    cred = await load_credentials(user_id)
    if cred is None:
        return None
    if not force_refresh and not cred.is_token_expired():
        return cred
    access_token, expires_at = await mint_access_token(
        client_id=cred.client_id,
        client_secret=cred.client_secret,
    )
    return await save_credentials(
        user_id,
        tenant_id=cred.tenant_id,
        client_id=cred.client_id,
        client_secret=cred.client_secret,
        app_key=cred.app_key,
        access_token=access_token,
        expires_at=expires_at,
    )
