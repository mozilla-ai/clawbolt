"""HTTP client for the ServiceTitan REST API.

Every ServiceTitan resource endpoint requires two headers:

* ``Authorization: Bearer <access_token>`` (per-tenant, 15-minute lifetime)
* ``ST-App-Key: <app_key>`` (operator-level, constant)

This module owns the request loop that injects both, refreshes the bearer
on 401, and routes through the in-process fake when
``settings.servicetitan_use_fake`` is true. Resource-specific helpers
(list customers, get job, etc.) live in the read-tools issue (#1300); this
scaffold ships the transport + auth refresh path and a bare ``get`` / ``post``
surface that downstream tools build on.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from backend.app.config import settings
from backend.app.integrations.servicetitan._fake import build_fake_transport
from backend.app.integrations.servicetitan.auth import (
    ServiceTitanAuthError,
    ServiceTitanCredential,
    get_valid_token,
)

logger = logging.getLogger(__name__)


# Per-request timeout. Generous so a real ServiceTitan cold-path TLS
# handshake plus a paginated list query never trips on a tight ceiling;
# the fake transport returns in microseconds either way.
_DEFAULT_TIMEOUT_SECONDS = 60.0


class ServiceTitanError(RuntimeError):
    """Generic ServiceTitan API failure (5xx, network, validation)."""


class ServiceTitanNotConnectedError(ServiceTitanError):
    """Raised when the service is built for a user with no credential.

    Callers should never hit this in normal flow: the factory's
    ``auth_check`` gates tool creation on a credential being present.
    Kept as a distinct subclass so a regression that bypasses the check
    is loud rather than surfaced as a generic 401.
    """


def _build_http_client(timeout_seconds: float) -> httpx.AsyncClient:
    """Construct an async client wired to the configured ServiceTitan host.

    Routes through the in-process fake when ``servicetitan_use_fake`` is
    on. The base_url is taken from settings in both modes so resource
    helpers can use relative paths.
    """
    timeout = httpx.Timeout(timeout_seconds)
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


class ServiceTitanService:
    """Async REST client bound to one user's tenant credential.

    Short-lived (one per agent turn); does not cache responses. The
    bearer token is refreshed lazily on 401 by re-running
    :func:`get_valid_token`, which mints a new client-credentials token
    against the configured token endpoint.
    """

    def __init__(
        self,
        user_id: str,
        credential: ServiceTitanCredential,
        *,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._user_id = user_id
        self._credential = credential
        self._timeout = timeout_seconds
        self._refreshed_once = False

    @property
    def credential(self) -> ServiceTitanCredential:
        return self._credential

    @property
    def tenant_id(self) -> str:
        return self._credential.tenant_id

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._credential.access_token}",
            "ST-App-Key": self._credential.app_key,
            "Accept": "application/json",
        }

    async def _refresh_credential(self, *, force: bool = False) -> bool:
        """Mint a fresh bearer for the current user.

        ``force=True`` skips the cached-expiry shortcut in
        :func:`get_valid_token`. Used by the 401 retry path: the server
        may have revoked a bearer before its declared ``expires_in``
        (e.g. a tenant rotated their secret), and the in-memory expiry
        would otherwise mask that.

        Returns True when the refresh succeeded and the credential's
        ``access_token`` has been updated in-place. Returns False when
        the credential is gone (deleted between calls) or the refresh
        endpoint failed; callers should treat that as a hard 401.
        """
        try:
            refreshed = await get_valid_token(self._user_id, force_refresh=force)
        except ServiceTitanAuthError as exc:
            logger.warning(
                "ServiceTitan token refresh failed for user=%s: %s",
                self._user_id,
                exc,
            )
            return False
        if refreshed is None:
            return False
        self._credential = refreshed
        return True

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> Any:
        """Send an authenticated request and return the parsed JSON body.

        Handles a one-shot refresh-and-retry on 401: if the stored bearer
        was rejected, we mint a fresh one and replay the request once.
        Subsequent 401s on the same service instance are surfaced as
        :class:`ServiceTitanError` so the caller can decide whether to
        clear the credential.
        """
        # Lazy mint: the service was built from a stored credential whose
        # bearer had already lapsed. Fetch one before sending so the first
        # call does not pay a guaranteed 401 round trip.
        if not self._credential.access_token and not await self._refresh_credential():
            raise ServiceTitanNotConnectedError(
                "ServiceTitan credential is not usable; reconnect required."
            )

        try:
            async with _build_http_client(self._timeout) as client:
                resp = await client.request(
                    method,
                    path,
                    params=params,
                    json=json_body,
                    headers=self._headers(),
                )
        except httpx.HTTPError as exc:
            logger.warning("ServiceTitan %s %s network failure: %s", method, path, exc)
            raise ServiceTitanError(
                f"ServiceTitan {method} {path} network failure: {exc!r}"
            ) from exc

        if resp.status_code == 401 and not self._refreshed_once:
            self._refreshed_once = True
            if await self._refresh_credential(force=True):
                try:
                    async with _build_http_client(self._timeout) as client:
                        resp = await client.request(
                            method,
                            path,
                            params=params,
                            json=json_body,
                            headers=self._headers(),
                        )
                except httpx.HTTPError as exc:
                    raise ServiceTitanError(
                        f"ServiceTitan {method} {path} network failure after refresh: {exc!r}"
                    ) from exc

        if resp.status_code >= 400:
            logger.warning(
                "ServiceTitan %s %s failed: status=%d body=%s",
                method,
                path,
                resp.status_code,
                resp.text[:500],
            )
            raise ServiceTitanError(f"ServiceTitan {method} {path} failed: HTTP {resp.status_code}")

        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise ServiceTitanError(f"ServiceTitan {method} {path} returned non-JSON body") from exc

    async def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        return await self.request("GET", path, params=params)

    async def post(
        self,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        return await self.request("POST", path, params=params, json_body=json_body)


async def build_service_for_user(
    user_id: str,
    *,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> ServiceTitanService | None:
    """Construct a :class:`ServiceTitanService` for the given user.

    Returns ``None`` when the user has no stored credential. Refreshes
    the bearer token eagerly via :func:`get_valid_token`, so the
    returned service is ready to issue resource calls without a
    guaranteed 401 round trip on the first request.
    """
    cred = await get_valid_token(user_id)
    if cred is None:
        return None
    return ServiceTitanService(user_id, cred, timeout_seconds=timeout_seconds)
