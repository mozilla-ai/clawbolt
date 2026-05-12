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


_DEFAULT_TIMEOUT_SECONDS = 120.0
"""Per-request timeout. Raised from 30s to 120s after observing
``add_work_order_note`` calls with multiple photos hit the original
ceiling: 6 photos at typical phone-camera resolution become ~15-25 MB
of base64 payload, and AppFolio's note endpoint then has to persist
each one. The longer ceiling costs nothing on the read paths (which
return in milliseconds) and keeps media-attaching writes from failing
on perfectly normal uploads."""


def _format_http_exception(exc: BaseException) -> str:
    """Return a non-empty description of an httpx exception.

    ``httpx.WriteTimeout()``, ``RemoteProtocolError()`` and friends are
    sometimes constructed with no message, in which case ``str(exc)``
    returns an empty string and we end up surfacing
    ``"network failure: "`` to the user. Fall back to the exception class
    name so the error is at least diagnosable.
    """
    msg = str(exc)
    if msg:
        return msg
    return type(exc).__name__


_CLIENT_VERSION = "b3b7f2f73bc52946cf8dab2e61208b9d37996479"
"""Sent in ``X-Vendor-Portal-Web-Client``. The SPA sends a 40-char hex
(looks like a git SHA); we mirror that shape with a stable random hex
value so the header doesn't identify which integrator is calling. The
value itself is opaque to AppFolio."""


class AppFolioError(RuntimeError):
    """Generic AppFolio API failure (5xx, network, validation)."""


class AuthExpiredError(AppFolioError):
    """JWT was rejected because the credential genuinely expired.

    The caller must prompt the user for a new magic link. AppFolio
    signals this by returning HTTP 401 with a ``login_url`` field in
    the body that the SPA would redirect the user to.

    ``login_url`` is that URL; tools relay it to the user.
    """

    def __init__(self, login_url: str = "") -> None:
        super().__init__("AppFolio session expired; a new magic link is required")
        self.login_url = login_url


class AuthScopeError(AppFolioError):
    """JWT is valid but not authorized for the customer in the request.

    AppFolio reuses HTTP 401 for two distinct cases: the JWT itself
    expired (``AuthExpiredError`` above, with a ``login_url`` payload),
    and the JWT is fine but the path's customer scope (or the body's
    ``customer_id``) does not match what the JWT was minted for. The
    second case is signalled by a 401 with no ``login_url`` in the
    body, and reconnecting will not help: the caller must retry with
    the correct customer instead.

    Tools that synthesize a customer_id (e.g. by guessing from a
    search response) catch this to fall back to the canonical
    ``/profiles/me`` value before surfacing the failure to the user.
    """


@dataclass
class FileUpload:
    """Binary file to attach to a note or invoice request body.

    AppFolio expects ``{file_in_base64, name}`` JSON entries rather than
    multipart form-data. ``data`` is the raw bytes; the caller does not
    need to base64-encode them.
    """

    name: str
    data: bytes


# Target raw-byte ceiling for individual photos uploaded to AppFolio.
# A typical work-order note attaches 1-6 photos in a single JSON body;
# at 4-5 MB raw each the request becomes ~25 MB once base64-inflated and
# routinely trips upload timeouts. 1.5 MB raw is plenty for documentary
# photos of property damage / completed work and keeps a six-photo note
# under ~12 MB on the wire.
_APPFOLIO_PHOTO_TARGET_BYTES = 1_500_000

_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}


def _looks_like_image(name: str) -> bool:
    lower = name.lower()
    return any(lower.endswith(ext) for ext in _IMAGE_EXTENSIONS)


def _maybe_compress_photo(name: str, data: bytes) -> bytes:
    """Compress a photo down to ``_APPFOLIO_PHOTO_TARGET_BYTES`` if needed.

    Non-image files and anything Pillow can't open pass through unchanged.
    Compression uses the same JPEG quality + resize ladder as the vision
    pipeline (see :func:`backend.app.media.vision.compress_image_for_api`)
    so behavior stays consistent across the codebase.
    """
    if not _looks_like_image(name):
        return data
    if len(data) <= _APPFOLIO_PHOTO_TARGET_BYTES:
        return data
    try:
        from backend.app.media.vision import compress_image_for_api

        compressed, _ = compress_image_for_api(
            data, "image/jpeg", max_raw_bytes=_APPFOLIO_PHOTO_TARGET_BYTES
        )
        logger.info(
            "AppFolio photo %s compressed: %d -> %d bytes", name, len(data), len(compressed)
        )
        return compressed
    except Exception as exc:
        # Pillow can't open every format (HEIC without pillow-heif, PSD,
        # etc.). Don't fail the upload over a compression miss; AppFolio
        # may still accept the original.
        logger.warning("AppFolio photo %s could not be compressed (%s); uploading as-is", name, exc)
        return data


def _encode_files(files: list[FileUpload]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for f in files:
        data = _maybe_compress_photo(f.name, f.data)
        out.append({"name": f.name, "file_in_base64": base64.b64encode(data).decode("ascii")})
    return out


_LOG_BODY_PREVIEW_LIMIT = 1500
"""Cap on response/request body length surfaced in error logs and the
:class:`AppFolioError` message. Long enough to capture a JSON validation
error in full; short enough to keep individual log lines manageable."""


def _summarize_body_for_log(body: Any) -> Any:
    """Replace base64-encoded file payloads with size markers for log output.

    AppFolio note/invoice bodies inline photo bytes as base64 strings,
    which dwarf the rest of the JSON and bury the actual API contract
    we're trying to debug. Replace each ``file_in_base64`` string with a
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
    """Replace a ``{file_in_base64, name}`` dict's payload with a size marker."""
    if not isinstance(entry, dict):
        return entry
    summarized: dict[str, Any] = {}
    for k, v in entry.items():
        if k == "file_in_base64" and isinstance(v, str):
            summarized[k] = f"<{len(v)} chars base64>"
        else:
            summarized[k] = v
    return summarized


def _summarize_files_field(body: Any) -> Any:
    """Specialize the generic summarizer for AppFolio's file payload shapes.

    Handles both the plural form (``files: [{file_in_base64, name}, ...]``,
    used by notes and invoices) and the singular form (``file: {file_in_base64,
    name}``, kept generic so any future single-file endpoint logs cleanly).
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
            raise AppFolioError(
                f"AppFolio {method} {path} network failure: {_format_http_exception(exc)}"
            ) from exc

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
                # AppFolio returns 401 for two distinct cases. When the
                # JWT itself expired the body carries a ``login_url`` for
                # the SPA to redirect the user to. When the JWT is valid
                # but the path's customer scope or body ``customer_id``
                # does not match the JWT's bound customer, the body has
                # no ``login_url`` and reconnecting will not help. Tell
                # the caller which one happened so they can react
                # accordingly: write tools that guess customer_id (e.g.
                # from a search response) catch the scope variant and
                # retry with the canonical ``/profiles/me`` value.
                if login_url:
                    raise AuthExpiredError(login_url=login_url)
                raise AuthScopeError(
                    f"AppFolio {method} {path} rejected the request scope (401);"
                    " the JWT is valid but not authorized for this customer."
                )
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
            # Don't echo response_text into the raised error: AppFolioError
            # messages flow into ToolResult.content (visible to the LLM and
            # the end user). The full body stays in the warning log above.
            raise AppFolioError(f"AppFolio {method} {path} failed: HTTP {resp.status_code}")
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
    # Work orders
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
            params={"search_term": search_term},
        )

    async def get_work_order(self, customer_id: str, work_order_id: str) -> Any:
        return await self.get(f"/work_order/{customer_id}/{work_order_id}.json")

    async def get_work_order_details(self, work_order_id: str) -> Any:
        return await self.get(f"/maintenance/api/work_orders/{work_order_id}/work_order_details")

    async def get_profile(self) -> Any:
        return await self.get("/profiles/me", params={"viewed": "true"})

    async def _resolve_primary_customer_id(self) -> str:
        """Return the vendor's primary customer ID.

        AppFolio's note POST and several other write endpoints require a
        ``customer_id`` (the property manager's ID) at the request level.
        The legacy ``/access`` exchange returned this in the body and we
        cached it on the credential; the new OAuth2 endpoint does not, so
        existing connected users have ``customer_ids=[]`` on their
        persisted credential. Fall back to ``/profiles/me`` for those
        cases and stash the result on the credential so we don't refetch
        every call within the same service-instance lifetime.

        Single-customer vendors (the common case) get the lone customer
        back. Multi-customer vendors get the first one; tools that need
        a specific customer should accept it as a parameter and bypass
        this helper.
        """
        if self._credential.customer_ids:
            return str(self._credential.customer_ids[0])
        profile = await self.get_profile()
        ids: list[str] = []
        if isinstance(profile, dict):
            customers = profile.get("customers") or []
            if isinstance(customers, list):
                for c in customers:
                    if isinstance(c, dict):
                        cid = c.get("customer_id") or c.get("customerId")
                        if cid is not None:
                            s = str(cid)
                            if s not in ids:
                                ids.append(s)
        if not ids:
            raise AppFolioError(
                "AppFolio /profiles/me returned no customer IDs; cannot make this request"
            )
        # Cache on the in-memory credential for the rest of this turn.
        # Persistence to oauth_tokens.extra_json happens at next refresh.
        self._credential.customer_ids = ids
        return ids[0]

    # ------------------------------------------------------------------
    # Work-order notes
    # ------------------------------------------------------------------

    async def list_work_order_notes(self, work_order_id: str) -> Any:
        return await self.get(f"/maintenance/api/work_orders/{work_order_id}/notes")

    async def add_work_order_note(
        self,
        work_order_id: str,
        *,
        body_text: str,
        files: list[FileUpload] | None = None,
        customer_id: str | None = None,
    ) -> Any:
        """POST a note (text + optional photos) onto a work order.

        AppFolio's note endpoint requires ``customer_id`` (the property
        manager's ID) at the request top level. The legacy ``/access``
        flow returned this on the credential and we cached it; the new
        OAuth2 flow does not, so we resolve it via ``/profiles/me`` when
        the caller does not pass one explicitly. Without ``customer_id``
        AppFolio rejects with a 422 + empty body.
        """
        cid = customer_id or await self._resolve_primary_customer_id()
        body: dict[str, Any] = {
            "note": {"body": body_text},
            "files": _encode_files(files) if files else [],
            "customer_id": cid,
        }
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
        customer_id: str | None = None,
    ) -> Any:
        """PATCH an existing note on a work order.

        Body shape mirrors :meth:`add_work_order_note` (which was
        Playwright-verified in PR #1277). The PATCH verb itself and
        its full-body-replacement semantics are extrapolated from
        REST convention and the matching POST shape, **not**
        Playwright-verified. AppFolio may instead expect a partial
        body (e.g. only the changed fields) or a different verb;
        revisit if it rejects.
        """
        cid = customer_id or await self._resolve_primary_customer_id()
        body: dict[str, Any] = {
            "note": {"body": body_text},
            "files": _encode_files(files) if files else [],
            "customer_id": cid,
        }
        return await self.patch(
            f"/maintenance/api/work_orders/{work_order_id}/notes/{note_id}",
            json_body=body,
        )

    # ------------------------------------------------------------------
    # Invoices
    # ------------------------------------------------------------------

    async def create_invoice(
        self,
        *,
        customer_id: str | None = None,
        work_order_id: str,
        line_items: list[dict[str, Any]],
        address: dict[str, Any] | None = None,
        reference_number: str = "",
        files: list[FileUpload] | None = None,
    ) -> Any:
        """Create a line-itemized invoice tied to a work order.

        SPA-verified body shape (snake_case throughout):

        ``{customer_id, work_order_id (int), line_items: [{amount, description,
        quantity}], address: {property_or_unit_name, address_1, address_2,
        city, state, zip_code}, reference_number}``

        ``line_items`` entries use ``amount`` (not ``rate``) and the SPA sends
        ``quantity`` as a string. The wire ``amount`` is the **line total**
        (unit price * quantity); AppFolio stores it as-is and does not
        re-multiply by ``quantity``. The line-itemized tool wrapper does
        the multiplication before calling this method, so callers passing
        their own ``line_items`` dicts must do the same. ``address`` is
        sourced from the work-order location and is part of the SPA's
        payload; we pass it through so the invoice prints the correct
        property block. ``reference_number`` is the vendor-side invoice
        number (the SPA auto-suggests one based on the WO).

        ``customer_id`` is optional. Pass ``None`` to resolve the canonical
        property-manager ID from ``/profiles/me``; this is the safe default
        when the agent's only signal is a search response (which can carry
        a different ``customer_id`` field than the write endpoints expect).
        Mirrors :meth:`add_work_order_note` and the other write helpers.
        """
        cid = customer_id or await self._resolve_primary_customer_id()
        body: dict[str, Any] = {
            "customer_id": str(cid),
            "work_order_id": int(work_order_id) if str(work_order_id).isdigit() else work_order_id,
            "line_items": line_items,
        }
        if address:
            body["address"] = address
        if reference_number:
            body["reference_number"] = reference_number
        if files:
            body["files"] = _encode_files(files)
        return await self.post("/maintenance/api/invoices", json_body=body)

    async def upload_invoice_pdf(
        self,
        *,
        customer_id: str | None = None,
        work_order_id: str,
        files: list[FileUpload],
        address: dict[str, Any] | None = None,
        reference_number: str = "",
    ) -> Any:
        """Upload one or more pre-built invoice PDFs as a single AppFolio invoice.

        Uses the same ``/maintenance/api/invoices`` endpoint as line-itemized
        invoices; the SPA distinguishes by sending ``files`` without
        ``line_items``. ``customer_id`` follows the same optional-with-fallback
        pattern as :meth:`create_invoice`.
        """
        if not files:
            raise ValueError("upload_invoice_pdf requires at least one file")
        cid = customer_id or await self._resolve_primary_customer_id()
        body: dict[str, Any] = {
            "customer_id": str(cid),
            "work_order_id": int(work_order_id) if str(work_order_id).isdigit() else work_order_id,
            "files": _encode_files(files),
        }
        if address:
            body["address"] = address
        if reference_number:
            body["reference_number"] = reference_number
        return await self.post("/maintenance/api/invoices", json_body=body)


def build_service(
    credential: AppFolioCredential,
    *,
    api_base: str,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    on_token_refresh: Callable[[str, str], Awaitable[None]] | None = None,
) -> AppFolioVendorService:
    """Construct an :class:`AppFolioVendorService` for a credential."""
    if not credential.jwt:
        raise ValueError("AppFolio credential has no JWT")
    if not credential.fingerprint:
        raise ValueError("AppFolio credential has no fingerprint")
    return AppFolioVendorService(
        credential,
        api_base=api_base,
        timeout_seconds=timeout_seconds,
        on_token_refresh=on_token_refresh,
    )


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
    raw: dict[str, Any]
    refresh_token: str = ""


async def exchange_magic_link(
    *,
    magic_link_token: str,
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
        raise AppFolioError(
            f"AppFolio OAuth exchange network failure: {_format_http_exception(exc)}"
        ) from exc
    if resp.status_code >= 400:
        response_text = resp.text[:_LOG_BODY_PREVIEW_LIMIT]
        logger.warning(
            "AppFolio OAuth exchange failed: status=%d response=%s",
            resp.status_code,
            response_text,
        )
        # Body stays in the log; the raised message is shown to the user
        # via ToolResult.content, so we keep it status-only.
        raise AppFolioError(f"AppFolio OAuth exchange failed: HTTP {resp.status_code}")
    payload: dict[str, Any] = resp.json() if resp.content else {}
    jwt = payload.get("access_token") or ""
    if not jwt:
        raise AppFolioError(
            f"AppFolio OAuth exchange returned no access_token (keys: {sorted(payload.keys())})"
        )
    return AccessExchangeResult(
        jwt=jwt,
        customer_ids=[],
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
        raise AppFolioError(
            f"AppFolio OAuth refresh network failure: {_format_http_exception(exc)}"
        ) from exc
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
        raw=payload,
        refresh_token=payload.get("refresh_token") or refresh_token,
    )
