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
from collections.abc import Awaitable, Callable
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


_LOG_BODY_PREVIEW_LIMIT = 1500
"""Cap on response/request body length surfaced in error logs and the
:class:`AppFolioError` message. Long enough to capture a JSON validation
error in full; short enough to keep individual log lines manageable."""


def _summarize_body_for_log(body: Any) -> Any:
    """Replace base64-encoded file payloads with size markers for log output.

    AppFolio note/invoice bodies inline photo bytes as base64 strings,
    which dwarf the rest of the JSON and bury the actual API contract
    we're trying to debug. Replace each ``file_base64`` string with a
    ``<base64 N bytes>`` marker; leave everything else intact.
    """
    if isinstance(body, dict):
        return {k: _summarize_body_for_log(v) for k, v in body.items()}
    if isinstance(body, list):
        return [_summarize_body_for_log(v) for v in body]
    if isinstance(body, str) and len(body) > 200:
        return f"<{len(body)} char string>"
    return body


def _summarize_file_entry(entry: Any) -> Any:
    """Replace a ``{file_base64, name}`` dict's payload with a size marker."""
    if not isinstance(entry, dict):
        return entry
    summarized: dict[str, Any] = {}
    for k, v in entry.items():
        if k == "file_base64" and isinstance(v, str):
            summarized[k] = f"<{len(v)} chars base64>"
        else:
            summarized[k] = v
    return summarized


def _summarize_files_field(body: Any) -> Any:
    """Specialize the generic summarizer for AppFolio's file payload shapes.

    Handles both the plural form (``files: [{file_base64, name}, ...]``,
    used by notes and invoices) and the singular form (``file: {file_base64,
    name}``, used by compliance uploads).
    """
    if not isinstance(body, dict):
        return _summarize_body_for_log(body)
    out: dict[str, Any] = {}
    for k, v in body.items():
        if k == "files" and isinstance(v, list):
            out[k] = [_summarize_file_entry(e) for e in v]
        elif k == "file" and isinstance(v, dict):
            out[k] = _summarize_file_entry(v)
        else:
            out[k] = _summarize_body_for_log(v)
    return out


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
        on_token_refresh: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        self._credential = credential
        self._api_base = api_base.rstrip("/")
        self._timeout = timeout_seconds
        self._on_token_refresh = on_token_refresh
        self._refreshed_once = False

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
        body_for_log = _summarize_files_field(json_body) if json_body is not None else None
        logger.debug(
            "AppFolio request: %s %s params=%r body=%r",
            method,
            path,
            params,
            body_for_log,
        )
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.request(
                    method,
                    url,
                    headers=self._headers(),
                    params=params,
                    json=json_body,
                )
        except httpx.HTTPError as exc:
            logger.warning(
                "AppFolio %s %s network failure: %s | params=%r body=%r",
                method,
                path,
                exc,
                params,
                body_for_log,
            )
            raise AppFolioError(f"AppFolio {method} {path} network failure: {exc}") from exc

        if resp.status_code == 401:
            # One-shot refresh-and-retry when we have a refresh_token on file.
            if self._credential.refresh_token and not self._refreshed_once:
                self._refreshed_once = True
                try:
                    refreshed = await refresh_access_token(
                        refresh_token=self._credential.refresh_token,
                        timeout_seconds=self._timeout,
                    )
                except AppFolioError:
                    pass
                else:
                    self._credential.jwt = refreshed.jwt
                    self._credential.refresh_token = (
                        refreshed.refresh_token or self._credential.refresh_token
                    )
                    if self._on_token_refresh:
                        await self._on_token_refresh(
                            self._credential.jwt, self._credential.refresh_token
                        )
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
                logger.warning(
                    "AppFolio %s %s rejected the JWT (401) | login_url=%r"
                    " | params=%r body=%r response=%s",
                    method,
                    path,
                    login_url,
                    params,
                    body_for_log,
                    resp.text[:_LOG_BODY_PREVIEW_LIMIT],
                )
                raise AuthExpiredError(login_url=login_url)
        if resp.status_code >= 400:
            response_text = resp.text[:_LOG_BODY_PREVIEW_LIMIT]
            logger.warning(
                "AppFolio %s %s failed: status=%d | params=%r body=%r response=%s",
                method,
                path,
                resp.status_code,
                params,
                body_for_log,
                response_text,
            )
            raise AppFolioError(
                f"AppFolio {method} {path} failed: {resp.status_code} {response_text}"
            )
        logger.debug(
            "AppFolio %s %s ok (%d, %d bytes)",
            method,
            path,
            resp.status_code,
            len(resp.content or b""),
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

    # ------------------------------------------------------------------
    # Domain helpers (PR3: invoices, compliance, estimates, profile)
    # ------------------------------------------------------------------

    async def create_invoice(
        self,
        *,
        customer_id: str,
        work_order_id: str,
        line_items: list[dict[str, Any]],
        invoice_number: str = "",
        due_date: str = "",
        files: list[FileUpload] | None = None,
    ) -> Any:
        """Create a line-itemized invoice tied to a work order.

        ``line_items`` should be a list of ``{description, quantity, rate}``
        entries (or whichever shape AppFolio's UI emits — confirmed
        post-smoke-test). Optional ``files`` attach photos or supporting
        docs inline as base64. ``invoice_number`` and ``due_date``
        (ISO YYYY-MM-DD) are passed through when present.
        """
        body: dict[str, Any] = {
            "customerId": customer_id,
            "workOrderId": work_order_id,
            "lineItems": line_items,
        }
        if invoice_number:
            body["invoiceNumber"] = invoice_number
        if due_date:
            body["dueDate"] = due_date
        if files:
            body["files"] = _encode_files(files)
        return await self.post("/maintenance/api/invoices", json_body=body)

    async def upload_invoice_pdf(
        self,
        *,
        customer_id: str,
        work_order_id: str,
        files: list[FileUpload],
    ) -> Any:
        """Upload one or more pre-built invoice PDFs as a single AppFolio invoice.

        AppFolio reuses the ``/maintenance/api/invoices`` endpoint for
        both shapes; the absence of ``lineItems`` plus presence of
        ``files`` switches it to the "uploaded PDF" mode.
        """
        if not files:
            raise ValueError("upload_invoice_pdf requires at least one file")
        return await self.post(
            "/maintenance/api/invoices",
            json_body={
                "customerId": customer_id,
                "workOrderId": work_order_id,
                "files": _encode_files(files),
            },
        )

    async def upload_compliance_document(
        self,
        *,
        customer_id: str,
        compliance_type: str,
        file: FileUpload,
    ) -> Any:
        """Upload a compliance doc (W-9, COI, license) for one customer.

        Note the singular ``file`` field; AppFolio's compliance endpoint
        does not take an array.
        """
        return await self.post(
            "/maintenance/api/compliance_documents",
            json_body={
                "customerId": customer_id,
                "complianceType": compliance_type,
                "file": {
                    "name": file.name,
                    "file_base64": base64.b64encode(file.data).decode("ascii"),
                },
            },
        )

    async def get_estimate(self, estimate_id: str) -> Any:
        return await self.get(
            f"/api/estimates/{estimate_id}",
            params={"include": "attachments"},
        )

    async def update_estimate(
        self,
        estimate_id: str,
        *,
        attributes: dict[str, Any],
    ) -> Any:
        """PATCH an estimate's attributes via JSON:API envelope.

        The frontend wraps the payload as ``{data: {id, type: "estimates",
        attributes: ...}}``. We mirror that shape; the agent supplies just
        the attributes dict.
        """
        return await self.patch(
            f"/api/estimates/{estimate_id}",
            json_body={
                "data": {
                    "id": estimate_id,
                    "type": "estimates",
                    "attributes": attributes,
                }
            },
        )

    async def update_profile(
        self,
        *,
        first_name: str | None = None,
        last_name: str | None = None,
        phone_number: str | None = None,
        company_name: str | None = None,
    ) -> Any:
        """PATCH /profiles with whatever subset of fields the user wants changed.

        Mirror the SPA shape (``user: {...}, company: {...}``) but only
        include each sub-object when at least one of its fields is set,
        so we don't send empty objects that AppFolio could read as
        "clear these values".
        """
        user: dict[str, Any] = {}
        if first_name is not None:
            user["firstName"] = first_name
        if last_name is not None:
            user["lastName"] = last_name
        if phone_number is not None:
            user["phoneNumber"] = phone_number
        company: dict[str, Any] = {}
        if company_name is not None:
            company["name"] = company_name
        body: dict[str, Any] = {}
        if user:
            body["user"] = user
        if company:
            body["company"] = company
        if not body:
            raise ValueError("update_profile requires at least one field to change")
        return await self.patch("/profiles", json_body=body)


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


# OAuth2 token endpoint that mints vendor-portal bearer JWTs from a
# magic-link token. Replaces the legacy ``vendor.appf.io/access`` flow.
OAUTH_TOKEN_URL = "https://oauth.appf.io/oauth/token"
_OAUTH_CLIENT_ID = "passport-frontend"


@dataclass
class AccessExchangeResult:
    """Outcome of a successful magic-link exchange."""

    jwt: str
    customer_ids: list[str]
    requires_two_factor: bool
    raw: dict[str, Any]
    refresh_token: str = ""


async def exchange_magic_link(
    *,
    api_base: str,
    magic_link_token: str,
    fingerprint: str = "",
    nfo: dict[str, Any] | None = None,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> AccessExchangeResult:
    """Exchange a magic-link token for a Bearer JWT via the OAuth2 endpoint."""
    body = {
        "vhost": "vendor",
        "property_token_credential": magic_link_token,
        "idp_type": "vendor",
        "client_id": _OAUTH_CLIENT_ID,
        "grant_type": "password",
        "require_reverification": True,
        "sync_phone_numbers": True,
    }
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    logger.debug("AppFolio OAuth exchange starting (token redacted)")
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            resp = await client.post(OAUTH_TOKEN_URL, headers=headers, json=body)
    except httpx.HTTPError as exc:
        logger.warning("AppFolio OAuth exchange network failure: %s", exc)
        raise AppFolioError(f"AppFolio OAuth exchange network failure: {exc}") from exc
    if resp.status_code >= 400:
        response_text = resp.text[:_LOG_BODY_PREVIEW_LIMIT]
        logger.warning(
            "AppFolio OAuth exchange failed: status=%d response=%s",
            resp.status_code,
            response_text,
        )
        raise AppFolioError(f"AppFolio OAuth exchange failed: {resp.status_code} {response_text}")
    payload: dict[str, Any] = resp.json() if resp.content else {}
    jwt = payload.get("access_token") or ""
    if not jwt:
        raise AppFolioError(
            f"AppFolio OAuth exchange returned no access_token (keys: {sorted(payload.keys())})"
        )
    return AccessExchangeResult(
        jwt=jwt,
        customer_ids=[],
        requires_two_factor=False,
        raw=payload,
        refresh_token=payload.get("refresh_token") or "",
    )


async def refresh_access_token(
    *,
    refresh_token: str,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> AccessExchangeResult:
    """Refresh an expired bearer JWT via the OAuth2 refresh-token grant."""
    body = {
        "client_id": _OAUTH_CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            resp = await client.post(OAUTH_TOKEN_URL, headers=headers, json=body)
    except httpx.HTTPError as exc:
        logger.warning("AppFolio OAuth refresh network failure: %s", exc)
        raise AppFolioError(f"AppFolio OAuth refresh network failure: {exc}") from exc
    if resp.status_code >= 400:
        response_text = resp.text[:_LOG_BODY_PREVIEW_LIMIT]
        logger.warning(
            "AppFolio OAuth refresh failed: status=%d response=%s",
            resp.status_code,
            response_text,
        )
        raise AuthExpiredError()
    payload: dict[str, Any] = resp.json() if resp.content else {}
    jwt = payload.get("access_token") or ""
    if not jwt:
        raise AuthExpiredError()
    return AccessExchangeResult(
        jwt=jwt,
        customer_ids=[],
        requires_two_factor=False,
        raw=payload,
        refresh_token=payload.get("refresh_token") or refresh_token,
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
    logger.debug("AppFolio 2FA onboard starting (code redacted)")
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            resp = await client.post(url, headers=headers, json=body)
    except httpx.HTTPError as exc:
        logger.warning("AppFolio 2FA onboard network failure: %s", exc)
        raise AppFolioError(f"AppFolio 2FA network failure: {exc}") from exc
    if resp.status_code >= 400:
        response_text = resp.text[:_LOG_BODY_PREVIEW_LIMIT]
        logger.warning(
            "AppFolio 2FA onboard failed: status=%d response=%s",
            resp.status_code,
            response_text,
        )
        raise AppFolioError(f"AppFolio 2FA onboard failed: {resp.status_code} {response_text}")
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
