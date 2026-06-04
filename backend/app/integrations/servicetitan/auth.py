"""ServiceTitan credential storage and client-credentials token minting.

ServiceTitan exposes an OAuth 2.0 ``client_credentials`` grant rather than
a user-consent flow: each tenant gives the integrator a Client ID, a
Client Secret, and a Tenant ID, and the integrator separately holds an
app-level App Key that goes in the ``ST-App-Key`` header on every call.

This module persists those four values in the ``oauth_tokens`` row for
``integration='servicetitan'``:

* ``access_token`` (encrypted) carries the current bearer token. Empty
  before the first mint, repopulated on every refresh.
* ``refresh_token`` (encrypted) carries the tenant's Client Secret. The
  column is named for OAuth refresh tokens, but ServiceTitan's client-
  credentials grant does not use one, so the encrypted slot is unused
  by the standard refresh flow and is the natural home for the secret.
  Storing it here keeps the Client Secret envelope-encrypted at rest.
* ``expires_at`` carries the absolute Unix timestamp at which the bearer
  expires. The real ServiceTitan token lives 15 minutes; we honor the
  ``expires_in`` returned by the token endpoint.
* ``extra_json`` (plaintext) carries the non-secret metadata:
  ``{tenant_id, client_id, app_key}``. The App Key is operator-level,
  not per-tenant; it is snapshotted here so a later operator-level
  rotation does not silently break existing tenants until they reconnect.

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

# Fallback lifetime when the token endpoint omits ``expires_in``. Sized to
# leave a meaningful usable window after ``_TOKEN_EXPIRY_BUFFER_SECONDS`` so
# a missing field does not turn into a tight mint loop, but still short
# enough that the bearer ages out quickly when no expiry was advertised.
_TOKEN_FALLBACK_TTL_SECONDS = 180.0


class ServiceTitanAuthError(RuntimeError):
    """Raised when token minting against ServiceTitan fails.

    Surfaces to the user via the connect flow when the pasted credentials
    are wrong, and via the refresh path when an existing credential stops
    working (e.g. the tenant rotated the secret).
    """


class ServiceTitanUnavailableError(ServiceTitanAuthError):
    """Raised when the failure is upstream (network or ServiceTitan 5xx).

    Distinct from the base class so callers can tell "the user's credentials
    are wrong" (a 4xx, the user's problem) from "ServiceTitan is down or
    unreachable" (a transient outage). The web connect endpoint maps this to
    HTTP 502 instead of 400 so a vendor outage does not read as bad input.
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
    """Return an httpx client wired to the ServiceTitan auth host.

    ServiceTitan serves ``/connect/token`` on a dedicated auth host
    (``auth.servicetitan.io`` in production, ``auth-integration.servicetitan.io``
    in the integration sandbox) that is distinct from the resource host
    on ``settings.servicetitan_api_base_url``. The fake's MockTransport
    accepts any host, so when ``servicetitan_use_fake`` is true the
    base_url here is irrelevant; the auth host setting still drives the
    production code path so the real environment works without further
    config changes.
    """
    timeout = httpx.Timeout(_TOKEN_REQUEST_TIMEOUT_SECONDS)
    if settings.servicetitan_use_fake:
        return httpx.AsyncClient(
            transport=build_fake_transport(),
            base_url=settings.servicetitan_auth_base_url,
            timeout=timeout,
        )
    return httpx.AsyncClient(
        base_url=settings.servicetitan_auth_base_url,
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
        raise ServiceTitanUnavailableError(
            f"ServiceTitan token endpoint unreachable: {exc!r}"
        ) from exc

    if resp.status_code >= 400:
        logger.warning(
            "ServiceTitan token mint failed: status=%d body=%s",
            resp.status_code,
            resp.text[:500],
        )
        if resp.status_code >= 500:
            # Upstream is broken, not the user's credentials.
            raise ServiceTitanUnavailableError(
                f"ServiceTitan token endpoint returned HTTP {resp.status_code}"
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
        # documented to as well. Fall back to a ceiling that leaves a
        # usable window after the safety buffer rather than 0 (which the
        # cache would treat as immortal) or a value so short the next
        # call churns the mint endpoint.
        expires_at = time.time() + _TOKEN_FALLBACK_TTL_SECONDS
    return access_token, expires_at


def _serialize_extra(cred: ServiceTitanCredential) -> dict[str, Any]:
    """Build the ``extra_json`` payload from a credential.

    Only non-secret metadata lives here. The Client Secret is persisted
    separately in the encrypted ``refresh_token`` column.
    """
    return {
        "tenant_id": cred.tenant_id,
        "client_id": cred.client_id,
        "app_key": cred.app_key,
    }


def _credential_from_token(token: OAuthTokenData) -> ServiceTitanCredential:
    """Reconstruct a credential from a loaded token row.

    The Client Secret comes off the encrypted ``refresh_token`` slot;
    everything else lives in ``extra_json``.
    """
    extra = token.extra or {}
    return ServiceTitanCredential(
        tenant_id=str(extra.get("tenant_id") or ""),
        client_id=str(extra.get("client_id") or ""),
        client_secret=token.refresh_token,
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
        # The Client Secret rides in the encrypted ``refresh_token`` slot.
        # ServiceTitan's client-credentials grant has no OAuth refresh
        # token, so this column is otherwise unused by the standard
        # refresh flow.
        refresh_token=client_secret,
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


async def connect_credentials(
    user_id: str,
    *,
    tenant_id: str,
    client_id: str,
    client_secret: str,
) -> ServiceTitanCredential:
    """Validate pasted ServiceTitan credentials and persist them.

    Strips the three values, mints a bearer against the token endpoint to
    prove they work, then persists them (with the operator-level App Key
    snapshotted in). Returns the saved credential.

    Raises :class:`ServiceTitanAuthError` when any field is blank, when the
    deployment is missing its App Key, or when ServiceTitan rejects the
    client credentials. Callers (the web connect endpoint) map that to a
    user-facing error.

    Connecting only happens through the authenticated web app: pasting
    these secrets into a chat thread would leave them in the message
    history where they cannot be cleared (issue #1337).
    """
    tenant_id = tenant_id.strip()
    client_id = client_id.strip()
    client_secret = client_secret.strip()
    if not tenant_id or not client_id or not client_secret:
        raise ServiceTitanAuthError("Tenant ID, Client ID, and Client Secret are all required.")
    if not settings.servicetitan_app_key:
        raise ServiceTitanAuthError(
            "ServiceTitan is not configured: the deployment is missing an App Key."
        )
    access_token, expires_at = await mint_access_token(
        client_id=client_id,
        client_secret=client_secret,
    )
    return await save_credentials(
        user_id,
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret=client_secret,
        app_key=settings.servicetitan_app_key,
        access_token=access_token,
        expires_at=expires_at,
    )


async def load_credentials(user_id: str) -> ServiceTitanCredential | None:
    """Load the user's persisted ServiceTitan credential, if any."""
    token = await oauth_service.load_token(user_id, INTEGRATION_NAME)
    return _build_credential_from_token(token)


async def load_credentials_uncached(user_id: str) -> ServiceTitanCredential | None:
    """Load the user's credential, bypassing the OAuth service's read cache.

    Use inside an advisory-lock critical section where the goal is to
    detect a peer worker's just-persisted refresh. The standard
    :func:`load_credentials` honors a 30-second TTL cache and would
    mask a cross-worker write within that window.
    """
    token = await oauth_service.load_token_uncached(user_id, INTEGRATION_NAME)
    return _build_credential_from_token(token)


def _build_credential_from_token(
    token: OAuthTokenData | None,
) -> ServiceTitanCredential | None:
    """Shared reconstruction + partial-row guard for both load_credentials paths."""
    if token is None:
        return None
    cred = _credential_from_token(token)
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

    Mint+save is serialized through ``oauth_service.refresh_lock`` so
    two concurrent requests for the same user do not race the token
    endpoint and clobber each other's persisted bearer (same primitive
    the standard OAuth refresh flow uses).

    Returns ``None`` when no credential exists, or when the advisory
    lock could not be acquired within its bounded wait (caller treats
    this as a transient refresh failure); raises
    :class:`ServiceTitanAuthError` when minting against the token endpoint
    fails (caller decides whether to clear the row).
    """
    cred = await load_credentials(user_id)
    if cred is None:
        return None
    if not force_refresh and not cred.is_token_expired():
        return cred

    async with oauth_service.refresh_lock(user_id, INTEGRATION_NAME) as acquired:
        if not acquired:
            logger.warning(
                "ServiceTitan refresh lock contended; skipping mint: user=%s",
                user_id,
            )
            return None
        # Re-check inside the lock: a peer worker may have just refreshed.
        # Bypass the in-memory load cache here; a stale entry from before
        # the lock would mask the peer's just-persisted token and trigger
        # a redundant mint.
        cred = await load_credentials_uncached(user_id)
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
