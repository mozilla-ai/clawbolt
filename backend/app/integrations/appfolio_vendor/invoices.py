"""Invoice tools for AppFolio Vendor Portal.

Two write paths share one endpoint:

* ``appfolio_create_invoice`` — line-itemized invoice built inside the
  portal. Supports inline photo attachments via ``media_refs``.
* ``appfolio_upload_invoice_pdf`` — single invoice constructed from one
  or more pre-built PDFs the user already has.

Both bodies POST to ``/maintenance/api/invoices``; AppFolio
disambiguates by the presence of ``lineItems`` vs ``files`` only.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from backend.app.agent.approval import ApprovalPolicy, PermissionLevel
from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolReceipt, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.integrations.appfolio_vendor.errors import service_error_to_tool_result
from backend.app.integrations.appfolio_vendor.media_resolver import resolve_staged_files
from backend.app.integrations.appfolio_vendor.params import (
    AppFolioCreateInvoiceParams,
    AppFolioInvoiceLineItem,
    AppFolioUploadInvoicePdfParams,
)
from backend.app.integrations.appfolio_vendor.service import (
    AppFolioVendorService,
    AuthScopeError,
)

if TYPE_CHECKING:
    from backend.app.agent.tools.registry import ToolContext

logger = logging.getLogger(__name__)


def _line_items_total(items: list[AppFolioInvoiceLineItem]) -> float:
    return sum(item.quantity * item.amount for item in items)


def _line_items_to_payload(items: list[AppFolioInvoiceLineItem]) -> list[dict[str, Any]]:
    """Match the SPA's invoice line-item shape exactly.

    SPA sends ``{amount, description, quantity}`` with ``quantity`` as a
    string. AppFolio's API rejects payloads with the older ``rate`` key.
    """
    return [
        {"amount": i.amount, "description": i.description, "quantity": str(i.quantity)}
        for i in items
    ]


# Canonical SPA address keys (in print order) mapped to the field names
# we look for on a work-order response. AppFolio's read endpoints ship
# both snake_case and camelCase variants and the production
# ``list_work_orders`` response nests the location under
# ``address: {propertyOrUnitName, address1, address2, city, state,
# zipCode}``, so we accept all of those forms.
_WO_ADDRESS_FIELD_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "property_or_unit_name",
        ("property_or_unit_name", "propertyOrUnitName", "unit_name", "property_name"),
    ),
    ("address_1", ("address_1", "address1", "street_address", "street")),
    ("address_2", ("address_2", "address2", "street_2")),
    ("city", ("city",)),
    ("state", ("state", "region")),
    ("zip_code", ("zip_code", "zipCode", "zip", "postal_code", "postalCode")),
)

# Container fields that may hold a nested address dict instead of the
# flat top-level fields. ``list_work_orders`` puts the address inside
# ``wo["address"]`` (a dict). Some envelope shapes might also wrap the
# whole work order at ``wo["work_order"]`` or ``wo["data"]``.
_WO_ADDRESS_CONTAINERS: tuple[str, ...] = ("address", "location", "propertyAddress")
_WO_TOP_CONTAINERS: tuple[str, ...] = ("work_order", "data")


def _address_from_work_order(wo: dict[str, Any]) -> dict[str, Any]:
    """Build the SPA-shaped invoice address block from a work-order dict.

    AppFolio's ``POST /maintenance/api/invoices`` returns HTTP 500 with
    an empty body when ``address`` is missing, even though the SPA
    payload documents it as optional. We sidestep that by fetching the
    work order and mapping its location fields into the SPA's expected
    shape.

    Resilient to three observed response shapes:

    * Address fields at the top level: ``wo.get("address_1")`` etc.
    * Nested under an ``address`` (or ``location`` / ``propertyAddress``)
      dict, the shape ``list_work_orders`` returns in production.
    * The whole work order wrapped in a ``work_order`` / ``data`` envelope.

    Returns an empty dict when nothing matches; the caller logs a
    diagnostic in that case (see :func:`_fetch_invoice_address`).
    """
    # If the WO is wrapped in a single-key envelope, unwrap it.
    for envelope_key in _WO_TOP_CONTAINERS:
        inner = wo.get(envelope_key)
        if isinstance(inner, dict) and inner:
            wo = inner
            break

    # Search both the top-level dict and any nested address container.
    sources: list[dict[str, Any]] = [wo]
    for container_key in _WO_ADDRESS_CONTAINERS:
        nested = wo.get(container_key)
        if isinstance(nested, dict) and nested:
            sources.append(nested)

    address: dict[str, Any] = {}
    for canonical, candidates in _WO_ADDRESS_FIELD_ALIASES:
        for source in sources:
            for name in candidates:
                value = source.get(name)
                if value:
                    address[canonical] = str(value)
                    break
            if canonical in address:
                break

    if not address:
        # Last-ditch: a flat string in one of the address container keys
        # (some endpoints return the formatted address as a single
        # string, not a dict). Send it as ``address_1`` so AppFolio at
        # least has something to print on the invoice.
        for container_key in (*_WO_ADDRESS_CONTAINERS, "property_address"):
            formatted = wo.get(container_key)
            if isinstance(formatted, str) and formatted:
                address["address_1"] = formatted
                break
    return address


async def _fetch_invoice_address(
    service: AppFolioVendorService, customer_id: str, work_order_id: str
) -> tuple[str, dict[str, Any]] | ToolResult:
    """Fetch the work-order address block plus the canonical customer_id.

    Returns ``(canonical_customer_id, address_dict)`` on success, or a
    populated :class:`ToolResult` on failure.

    AppFolio's invoice POST returns HTTP 500 with an empty body when
    ``address`` is missing, even though the SPA payload documents it as
    optional. We sidestep that by fetching the work order and mapping
    its location fields into the SPA's expected shape.

    The agent often arrives with the wrong ``customer_id`` because
    ``appfolio_search_work_orders`` returns a different ``customer_id``
    field than the write endpoints expect. ``GET /work_order/<cust>/<id>``
    answers HTTP 401 with no ``login_url`` for that case (raised as
    :class:`AuthScopeError`). We catch it, fall back to the canonical
    primary customer_id from ``/profiles/me``, and retry once. The
    canonical id is returned to the caller so the subsequent invoice
    POST can use the same value.
    """
    canonical_customer_id = customer_id
    try:
        wo = await service.get_work_order(canonical_customer_id, work_order_id)
    except AuthScopeError:
        # Wrong customer_id in the path; resolve the canonical one and
        # retry once. If resolution itself fails, surface that error.
        try:
            canonical_customer_id = await service._resolve_primary_customer_id()
        except Exception as exc:
            return service_error_to_tool_result("resolving the AppFolio customer for invoice", exc)
        try:
            wo = await service.get_work_order(canonical_customer_id, work_order_id)
        except Exception as exc:
            return service_error_to_tool_result("fetching work order address for invoice", exc)
    except Exception as exc:
        return service_error_to_tool_result("fetching work order address for invoice", exc)
    if not isinstance(wo, dict) or not wo:
        return ToolResult(
            content=f"Work order {work_order_id} not found.",
            is_error=True,
            error_kind=ToolErrorKind.NOT_FOUND,
        )
    address = _address_from_work_order(wo)
    if not address:
        # AppFolio rejects invoice POSTs without an address block (HTTP
        # 500 with an empty body), so an empty extraction here is going
        # to fail the invoice anyway. Log the WO key shape so the next
        # response variant we hit is debuggable, and surface a clear
        # error to the agent instead of letting the POST go out
        # half-formed.
        logger.warning(
            "AppFolio _address_from_work_order returned empty"
            " | work_order_id=%s top_keys=%r address_field_type=%s",
            work_order_id,
            sorted(wo.keys()),
            type(wo.get("address")).__name__,
        )
        return ToolResult(
            content=(
                f"AppFolio returned work order {work_order_id} but no"
                " address fields could be extracted; cannot build the"
                " invoice payload."
            ),
            is_error=True,
            error_kind=ToolErrorKind.SERVICE,
            hint=(
                "The work-order response shape may have changed. Check"
                " the warning log entry from the appfolio_vendor.invoices"
                " module for the field names AppFolio returned."
            ),
        )
    return canonical_customer_id, address


def build_invoice_tools(service: AppFolioVendorService, ctx: ToolContext) -> list[Tool]:
    """Return the AppFolio invoice tools."""

    async def appfolio_create_invoice(
        customer_id: str,
        work_order_id: str,
        line_items: list[dict[str, Any]],
        reference_number: str = "",
        media_refs: list[str] | None = None,
    ) -> ToolResult:
        # Pydantic-coerce raw dicts the LLM emits into the typed shape.
        try:
            typed_items = [AppFolioInvoiceLineItem.model_validate(li) for li in line_items]
        except Exception as exc:
            return ToolResult(
                content=f"Could not parse line items: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
                hint=(
                    "Each line item needs description (str), quantity (number),"
                    " and amount (number)."
                ),
            )
        if not typed_items:
            return ToolResult(
                content="At least one line item is required.",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )
        files_or_err = await resolve_staged_files(ctx, media_refs or [])
        if isinstance(files_or_err, ToolResult):
            return files_or_err
        files = files_or_err
        address_or_err = await _fetch_invoice_address(service, customer_id, work_order_id)
        if isinstance(address_or_err, ToolResult):
            return address_or_err
        canonical_customer_id, address = address_or_err
        try:
            result = await service.create_invoice(
                customer_id=canonical_customer_id,
                work_order_id=work_order_id,
                line_items=_line_items_to_payload(typed_items),
                address=address or None,
                reference_number=reference_number,
                files=files or None,
            )
        except Exception as exc:
            return service_error_to_tool_result("creating invoice", exc)

        invoice_id = ""
        if isinstance(result, dict):
            invoice_id = str(result.get("id") or result.get("invoice", {}).get("id") or "")
        total = _line_items_total(typed_items)
        photo_phrase = f" with {len(files)} attachment(s)" if files else ""
        return ToolResult(
            content=(
                f"Created invoice on work order {work_order_id} for ${total:.2f}"
                f" ({len(typed_items)} line item(s)){photo_phrase}"
                + (f" (invoice id {invoice_id})." if invoice_id else ".")
            ),
            receipt=ToolReceipt(
                action="Created AppFolio invoice",
                target=f"#{work_order_id} ${total:.2f}{photo_phrase}",
            ),
        )

    async def appfolio_upload_invoice_pdf(
        customer_id: str,
        work_order_id: str,
        media_refs: list[str],
        reference_number: str = "",
    ) -> ToolResult:
        if not media_refs:
            return ToolResult(
                content="At least one PDF or photo reference is required.",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )
        files_or_err = await resolve_staged_files(ctx, media_refs)
        if isinstance(files_or_err, ToolResult):
            return files_or_err
        files = files_or_err
        if not files:
            return ToolResult(
                content="No usable files resolved from the provided references.",
                is_error=True,
                error_kind=ToolErrorKind.NOT_FOUND,
            )
        address_or_err = await _fetch_invoice_address(service, customer_id, work_order_id)
        if isinstance(address_or_err, ToolResult):
            return address_or_err
        canonical_customer_id, address = address_or_err
        try:
            result = await service.upload_invoice_pdf(
                customer_id=canonical_customer_id,
                work_order_id=work_order_id,
                files=files,
                address=address or None,
                reference_number=reference_number,
            )
        except Exception as exc:
            return service_error_to_tool_result("uploading invoice", exc)

        invoice_id = ""
        if isinstance(result, dict):
            invoice_id = str(result.get("id") or "")
        return ToolResult(
            content=(
                f"Uploaded {len(files)} file(s) as an invoice on work order"
                f" {work_order_id}" + (f" (invoice id {invoice_id})." if invoice_id else ".")
            ),
            receipt=ToolReceipt(
                action="Uploaded AppFolio invoice",
                target=f"#{work_order_id} ({len(files)} file)",
            ),
        )

    return [
        Tool(
            name=ToolName.APPFOLIO_CREATE_INVOICE,
            description=(
                "Build a line-itemized invoice on an AppFolio work order"
                " (with optional photo attachments)."
            ),
            function=appfolio_create_invoice,
            params_model=AppFolioCreateInvoiceParams,
            usage_hint=(
                "Confirm each line item's description, quantity, and amount"
                " with the user before submitting; this is a billing action."
            ),
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                description_builder=lambda args: (
                    f"Create invoice on AppFolio work order"
                    f" #{args.get('work_order_id', '?')}"
                    f" ({len(args.get('line_items') or [])} line item(s))"
                ),
            ),
        ),
        Tool(
            name=ToolName.APPFOLIO_UPLOAD_INVOICE_PDF,
            description=(
                "Upload one or more pre-built PDFs as an invoice on an AppFolio work order."
            ),
            function=appfolio_upload_invoice_pdf,
            params_model=AppFolioUploadInvoicePdfParams,
            usage_hint=(
                "Use when the user has already prepared an invoice document."
                " For line-item entry, use appfolio_create_invoice instead."
            ),
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                description_builder=lambda args: (
                    f"Upload invoice PDF to AppFolio work order"
                    f" #{args.get('work_order_id', '?')}"
                    f" ({len(args.get('media_refs') or [])} file)"
                ),
            ),
        ),
    ]
