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
from backend.app.integrations.appfolio_vendor.service import AppFolioVendorService

if TYPE_CHECKING:
    from backend.app.agent.tools.registry import ToolContext

logger = logging.getLogger(__name__)


def _line_items_total(items: list[AppFolioInvoiceLineItem]) -> float:
    return sum(item.quantity * item.rate for item in items)


def _line_items_to_payload(items: list[AppFolioInvoiceLineItem]) -> list[dict[str, Any]]:
    return [{"description": i.description, "quantity": i.quantity, "rate": i.rate} for i in items]


def build_invoice_tools(service: AppFolioVendorService, ctx: ToolContext) -> list[Tool]:
    """Return the AppFolio invoice tools."""

    async def appfolio_create_invoice(
        customer_id: str,
        work_order_id: str,
        line_items: list[dict[str, Any]],
        invoice_number: str = "",
        due_date: str = "",
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
                    "Each line item needs description (str), quantity (number), and rate (number)."
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
        try:
            result = await service.create_invoice(
                customer_id=customer_id,
                work_order_id=work_order_id,
                line_items=_line_items_to_payload(typed_items),
                invoice_number=invoice_number,
                due_date=due_date,
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
        try:
            result = await service.upload_invoice_pdf(
                customer_id=customer_id,
                work_order_id=work_order_id,
                files=files,
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
                "Confirm each line item's description, quantity, and rate with"
                " the user before submitting; this is a billing action."
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
