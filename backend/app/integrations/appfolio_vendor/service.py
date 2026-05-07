"""HTTP client for the AppFolio Vendor Portal API.

The SPA at ``vendor.appfolio.com`` is a Backend-for-Frontend (Datadog
service name ``vendorbff``) that fronts a Rails API at
``vendor.appf.io``. Most data calls hit the latter directly with a
Bearer JWT obtained via the magic-link exchange.

Key request conventions captured from the SPA bundle:

* ``Authorization: Bearer <jwt>``
* ``Content-Type: application/json``
* ``X-Requested-With: XMLHttpRequest`` (the API rejects requests without it)
* ``X-Fingerprint: <persisted fingerprint>``
* ``X-Vendor-Portal-Web-Client: <opaque client version string>``

There is no CSRF token, no request signing, and no per-request nonce.
A 401 response carries ``{"login_url": "..."}`` in the body, which the
SPA uses to redirect the user back through the magic-link flow; we
surface that as :class:`AuthExpiredError` so tools can prompt the user
for a fresh link.
"""

from __future__ import annotations

import base64
import contextlib
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from backend.app.integrations.appfolio_vendor.auth import AppFolioCredential

logger = logging.getLogger(__name__)


_DEFAULT_TIMEOUT_SECONDS = 30.0
_CLIENT_VERSION = "clawbolt-1"
"""Sent in ``X-Vendor-Portal-Web-Client``. Opaque to AppFolio; used
internally for log correlation if we need it."""


class AppFolioError(RuntimeError):
    """Generic AppFolio API failure (5xx, network, validation)."""


class AuthExpiredError(AppFolioError):
    """JWT was rejected. The caller must prompt the user for a new magic link.

    ``login_url`` is the URL AppFolio's API returned on 401, intended for
    the SPA to redirect the user to. Tools relay this to the user.
    """

    def __init__(self, login_url: str = "") -> None:
        super().__init__("AppFolio session expired; a new magic link is required")
        self.login_url = login_url


@dataclass
class FileUpload:
    """Binary file to attach to a note or invoice request body.

    AppFolio expects ``{file_base64, name}`` JSON entries rather than
    multipart form-data. ``data`` is the raw bytes; the caller does not
    need to base64-encode them.
    """

    name: str
    data: bytes


def _encode_files(files: list[FileUpload]) -> list[dict[str, str]]:
    return [
        {"name": f.name, "file_base64": base64.b64encode(f.data).decode("ascii")} for f in files
    ]


class AppFolioVendorService:
    """Async REST client bound to one user's credential.

    The service is short-lived (one per agent turn) and does not cache
    responses. Construct via :meth:`from_credential` so the timeout and
    base URL come from settings.
    """

    def __init__(
        self,
        credential: AppFolioCredential,
        api_base: str,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._credential = credential
        self._api_base = api_base.rstrip("/")
        self._timeout = timeout_seconds

    @property
    def credential(self) -> AppFolioCredential:
        return self._credential

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._credential.jwt}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "X-Fingerprint": self._credential.fingerprint,
            "X-Vendor-Portal-Web-Client": _CLIENT_VERSION,
        }
        if extra:
            headers.update(extra)
        return headers

    def _full_url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if not path.startswith("/"):
            path = "/" + path
        return f"{self._api_base}{path}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> Any:
        url = self._full_url(path)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.request(
                method,
                url,
                headers=self._headers(),
                params=params,
                json=json_body,
            )
        if resp.status_code == 401:
            login_url = ""
            with contextlib.suppress(Exception):
                login_url = resp.json().get("login_url") or ""
            raise AuthExpiredError(login_url=login_url)
        if resp.status_code >= 400:
            raise AppFolioError(
                f"AppFolio {method} {path} failed: {resp.status_code} {resp.text[:300]}"
            )
        if not resp.content:
            return None
        ctype = resp.headers.get("content-type", "")
        if "application/json" in ctype:
            return resp.json()
        return resp.content

    async def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        return await self._request("GET", path, params=params)

    async def post(
        self,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        return await self._request("POST", path, params=params, json_body=json_body)

    async def patch(self, path: str, *, json_body: Any = None) -> Any:
        return await self._request("PATCH", path, json_body=json_body)

    async def delete(self, path: str) -> Any:
        return await self._request("DELETE", path)

    # ------------------------------------------------------------------
    # Domain helpers (PR1: read surface + sentinel)
    # ------------------------------------------------------------------

    async def list_work_orders(
        self,
        *,
        include_in_progress: bool = True,
        include_completed: bool = False,
        include_estimates: bool = True,
        customer_id: str | None = None,
    ) -> Any:
        params: dict[str, Any] = {
            "includeInProgress": str(include_in_progress).lower(),
            "includeCompleted": str(include_completed).lower(),
            "includeEstimates": str(include_estimates).lower(),
        }
        if customer_id:
            params["customer_id"] = customer_id
        return await self.get("/maintenance/api/work_orders.json", params=params)

    async def search_work_orders(self, search_term: str) -> Any:
        return await self.get(
            "/api/v1/search/work_order_search",
            params={"searchTerm": search_term},
        )

    async def get_work_order(self, customer_id: str, work_order_id: str) -> Any:
        return await self.get(f"/work_order/{customer_id}/{work_order_id}.json")

    async def get_work_order_details(self, work_order_id: str) -> Any:
        return await self.get(f"/maintenance/api/work_orders/{work_order_id}/work_order_details")

    async def list_payments(
        self,
        *,
        posted_on: str | None = None,
        settlement_method: str | None = None,
    ) -> Any:
        filters: dict[str, str] = {}
        if posted_on:
            filters["posted_on"] = posted_on
        if settlement_method:
            filters["settlement_method"] = settlement_method
        params = {f"filter[{k}]": v for k, v in filters.items()} if filters else None
        return await self.get("/api/maintenance/vendor_portal_payable_payments", params=params)

    async def get_profile(self) -> Any:
        return await self.get("/profiles/me", params={"viewed": "true"})

    # ------------------------------------------------------------------
    # Domain helpers (PR2: write surface)
    # ------------------------------------------------------------------

    async def accept_work_order(
        self,
        work_order_id: str,
        *,
        body: dict[str, Any] | None = None,
    ) -> Any:
        return await self.post(
            f"/maintenance/api/work_orders/{work_order_id}/accept",
            params={"ref": "vendor_portal"},
            json_body=body,
        )

    async def schedule_work_order(
        self,
        work_order_id: str,
        *,
        scheduled_at: str,
        duration_minutes: int | None = None,
        notes: str = "",
    ) -> Any:
        """POST a schedule onto a work order.

        Body shape mirrors the SPA: a flat dict whose keys are camelCase.
        ``scheduled_at`` should be an ISO 8601 string with timezone (or
        local with offset) so AppFolio can render it correctly back to
        the property manager.
        """
        body: dict[str, Any] = {"scheduledAt": scheduled_at}
        if duration_minutes is not None:
            body["durationMinutes"] = duration_minutes
        if notes:
            body["notes"] = notes
        return await self.post(
            f"/maintenance/api/work_orders/{work_order_id}/schedule",
            json_body=body,
        )

    async def update_work_order_status(self, work_order_id: str, *, status_code: int) -> Any:
        return await self.patch(
            f"/maintenance/api/work_orders/{work_order_id}",
            json_body={"workOrder": {"statusCode": status_code}},
        )

    async def undo_work_order_status(
        self, work_order_id: str, *, previous_status: int | str
    ) -> Any:
        return await self.patch(
            f"/maintenance/api/work_orders/{work_order_id}/undo_status",
            json_body={"workOrder": {"status": previous_status}},
        )

    async def list_work_order_notes(self, work_order_id: str) -> Any:
        return await self.get(f"/maintenance/api/work_orders/{work_order_id}/notes")

    async def add_work_order_note(
        self,
        work_order_id: str,
        *,
        body_text: str,
        files: list[FileUpload] | None = None,
    ) -> Any:
        body: dict[str, Any] = {"note": {"body": body_text}}
        if files:
            body["files"] = _encode_files(files)
        return await self.post(
            f"/maintenance/api/work_orders/{work_order_id}/notes",
            json_body=body,
        )

    async def update_work_order_note(
        self,
        work_order_id: str,
        note_id: str,
        *,
        body_text: str,
        files: list[FileUpload] | None = None,
    ) -> Any:
        body: dict[str, Any] = {"note": {"body": body_text}}
        if files:
            body["files"] = _encode_files(files)
        return await self.patch(
            f"/maintenance/api/work_orders/{work_order_id}/notes/{note_id}",
            json_body=body,
        )

    async def get_proxy_number(self, work_order_id: str) -> Any:
        """Fetch AppFolio's anonymized proxy phone number for the tenant.

        Vendors message tenants via a proxy number AppFolio mints per
        work order. Calling this endpoint creates (or returns) the
        number; the SMS itself goes through ``message_tenant`` below.
        """
        return await self.get(f"/maintenance/api/work_orders/{work_order_id}/get_proxy_number")

    async def message_tenant(
        self,
        *,
        work_order_id: str,
        phone_number: str,
        message: str,
    ) -> Any:
        return await self.post(
            "/maintenance/api/tenant_vendor_conversations",
            json_body={
                "work_order_id": work_order_id,
                "phone_number": phone_number,
                "message": message,
            },
        )


def build_service(
    credential: AppFolioCredential,
    *,
    api_base: str,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> AppFolioVendorService:
    """Construct an :class:`AppFolioVendorService` for a credential."""
    if not credential.jwt:
        raise ValueError("AppFolio credential has no JWT")
    if not credential.fingerprint:
        raise ValueError("AppFolio credential has no fingerprint")
    return AppFolioVendorService(credential, api_base=api_base, timeout_seconds=timeout_seconds)


# ----------------------------------------------------------------------
# Magic-link exchange (used by auth_tools, kept here so the network
# layer is one module).
# ----------------------------------------------------------------------


@dataclass
class AccessExchangeResult:
    """Outcome of a successful ``/access`` call."""

    jwt: str
    customer_ids: list[str]
    requires_two_factor: bool
    raw: dict[str, Any]


async def exchange_magic_link(
    *,
    api_base: str,
    magic_link_token: str,
    fingerprint: str,
    nfo: dict[str, Any] | None = None,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> AccessExchangeResult:
    """Exchange a magic-link token for a Bearer JWT.

    The SPA passes the ``magic_link_token`` as a query parameter on the
    ``/access`` POST and the fingerprint plus an ``nfo`` JSON blob in the
    body. We mirror that shape; ``nfo`` is opaque to AppFolio (the SPA
    fills it with browser metadata) so a small ``{"client": "clawbolt"}``
    is sufficient.
    """
    url = f"{api_base.rstrip('/')}/access"
    body = {"fingerprint": fingerprint, "nfo": (nfo or {"client": "clawbolt"})}
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "X-Fingerprint": fingerprint,
        "X-Vendor-Portal-Web-Client": _CLIENT_VERSION,
        "Authorization": f"Bearer {magic_link_token}",
    }
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        resp = await client.post(
            url,
            headers=headers,
            params={"magic_link_token": magic_link_token},
            json=body,
        )
    if resp.status_code >= 400:
        raise AppFolioError(
            f"AppFolio /access exchange failed: {resp.status_code} {resp.text[:300]}"
        )
    payload: dict[str, Any] = resp.json() if resp.content else {}
    jwt = (
        payload.get("access_token")
        or payload.get("jwt")
        or payload.get("token")
        or _extract_bearer_from_headers(resp.headers)
        or ""
    )
    if not jwt:
        raise AppFolioError(
            "AppFolio /access did not return a bearer token"
            f" (response keys: {sorted(payload.keys())})"
        )
    customer_ids = _extract_customer_ids(payload)
    requires_2fa = bool(
        payload.get("requires_two_factor")
        or payload.get("two_factor_required")
        or payload.get("twoFactorRequired")
    )
    return AccessExchangeResult(
        jwt=jwt,
        customer_ids=customer_ids,
        requires_two_factor=requires_2fa,
        raw=payload,
    )


async def submit_two_factor(
    *,
    api_base: str,
    jwt: str,
    fingerprint: str,
    code: str,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Submit a 2FA code against ``/two_factor_authentication/onboard``.

    Some AppFolio tenants gate the first ``/access`` exchange behind a
    one-time code sent over SMS or email. Returns the full response
    payload so callers can inspect any updated tokens.
    """
    url = f"{api_base.rstrip('/')}/two_factor_authentication/onboard"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "X-Fingerprint": fingerprint,
        "X-Vendor-Portal-Web-Client": _CLIENT_VERSION,
        "Authorization": f"Bearer {jwt}",
    }
    body = {"twoFactorToken": {"twoFactorToken": code}}
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        resp = await client.post(url, headers=headers, json=body)
    if resp.status_code >= 400:
        raise AppFolioError(f"AppFolio 2FA onboard failed: {resp.status_code} {resp.text[:300]}")
    return resp.json() if resp.content else {}


def _extract_bearer_from_headers(headers: httpx.Headers) -> str:
    auth = headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def _extract_customer_ids(payload: dict[str, Any]) -> list[str]:
    """Pull customer IDs out of the /access response.

    AppFolio returns multiple shapes across endpoints (sometimes a flat
    list, sometimes nested under ``customers``, sometimes ``customer_ids``).
    Walk each known shape and dedupe in iteration order.
    """
    raw_lists: list[Any] = [
        payload.get("customer_ids"),
        payload.get("customerIds"),
        payload.get("customers"),
    ]
    out: list[str] = []
    for entry in raw_lists:
        if not entry:
            continue
        if isinstance(entry, list):
            for item in entry:
                if isinstance(item, str):
                    if item not in out:
                        out.append(item)
                elif isinstance(item, dict):
                    cid = item.get("id") or item.get("customer_id") or item.get("customerId")
                    if isinstance(cid, str) and cid not in out:
                        out.append(cid)
                    elif isinstance(cid, int):
                        s = str(cid)
                        if s not in out:
                            out.append(s)
    return out
