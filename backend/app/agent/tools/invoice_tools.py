"""Invoice generation and estimate-to-invoice conversion tools for the agent."""

from __future__ import annotations

import asyncio
import datetime
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from backend.app.agent.client_db import ClientStore, EstimateStore, InvoiceStore, make_client_slug
from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolResult
from backend.app.agent.tools.file_tools import build_folder_path
from backend.app.agent.tools.names import ToolName
from backend.app.config import settings
from backend.app.enums import EstimateStatus, InvoiceStatus
from backend.app.models import User
from backend.app.services.pdf_service import InvoicePDFData, generate_invoice_pdf
from backend.app.services.storage_service import StorageBackend

if TYPE_CHECKING:
    from backend.app.agent.tools.registry import ToolContext

PDF_BASE_DIR = Path(settings.pdf_storage_dir)

logger = logging.getLogger(__name__)


class InvoiceLineItemParams(BaseModel):
    """A single line item in an invoice."""

    description: str = Field(description="Description of the line item")
    quantity: float = Field(default=1, ge=0, description="Quantity")
    unit_price: float = Field(ge=0, description="Price per unit")


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class GenerateInvoiceParams(BaseModel):
    """Parameters for the generate_invoice tool."""

    description: str = Field(description="Brief description of the work")
    line_items: list[InvoiceLineItemParams] = Field(
        description="Line items for the invoice",
        min_length=1,
    )
    client_name: str | None = Field(default=None, description="Client name (optional)")
    client_address: str | None = Field(default=None, description="Client address (optional)")
    due_date: str | None = Field(
        default=None,
        description="Due date in YYYY-MM-DD format (optional)",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )
    notes: str | None = Field(default=None, description="Additional notes (optional)")


class ConvertEstimateToInvoiceParams(BaseModel):
    """Parameters for the convert_estimate_to_invoice tool."""

    estimate_id: str = Field(description="Estimate ID to convert (e.g. EST-0001)")
    due_date: str | None = Field(
        default=None,
        description="Due date in YYYY-MM-DD format (optional)",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )
    notes: str | None = Field(default=None, description="Additional notes (optional)")


async def _generate_invoice_pdf_and_save(
    user: User,
    invoice_id: str,
    description: str,
    processed_items: list[dict[str, Any]],
    subtotal: float,
    total_amount: float,
    client_name: str | None,
    client_address: str | None,
    client_slug: str | None,
    due_date: str | None,
    notes: str | None,
    storage: StorageBackend | None,
    invoice_store: InvoiceStore,
) -> str:
    """Generate invoice PDF, save locally and to cloud, update invoice record. Returns pdf_path."""
    today = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")

    pdf_data = InvoicePDFData(
        owner_name="User",
        owner_phone=user.phone or "",
        owner_trade="",
        description=description,
        line_items=processed_items,
        subtotal=subtotal,
        total=total_amount,
        invoice_date=today,
        invoice_number=invoice_id,
        client_name=client_name,
        client_address=client_address,
        due_date=due_date,
        notes=notes,
    )

    pdf_bytes = await generate_invoice_pdf(pdf_data)

    # Save PDF to local storage
    pdf_dir = PDF_BASE_DIR / str(user.id) / (client_slug or "unsorted")
    pdf_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = pdf_dir / f"{invoice_id}.pdf"
    await asyncio.to_thread(pdf_path.write_bytes, pdf_bytes)

    # Upload to cloud storage if available
    cloud_path = ""
    if storage:
        try:
            folder_path = build_folder_path("invoice", client_name, client_address)
            await storage.create_folder(folder_path)
            await storage.upload_file(pdf_bytes, folder_path, f"{invoice_id}.pdf")
            cloud_path = f"{folder_path}/{invoice_id}.pdf"
        except Exception:
            logger.warning(
                "Cloud upload failed for invoice %s, local PDF saved successfully",
                invoice_id,
            )

    # Update invoice with PDF path and cloud storage path
    update_fields: dict[str, str] = {"pdf_url": str(pdf_path)}
    if cloud_path:
        update_fields["storage_path"] = cloud_path
    await invoice_store.update(invoice_id, **update_fields)

    return str(pdf_path)


def create_invoice_tools(
    user: User,
    storage: StorageBackend | None = None,
) -> list[Tool]:
    """Create invoice-related tools for the agent."""

    async def generate_invoice(
        description: str,
        line_items: list[dict[str, Any]],
        client_name: str | None = None,
        client_address: str | None = None,
        due_date: str | None = None,
        notes: str | None = None,
    ) -> ToolResult:
        """Generate a professional invoice PDF and return summary."""
        # Calculate totals
        processed_items: list[dict[str, Any]] = []
        subtotal = 0.0
        for item in line_items:
            try:
                qty = float(item.get("quantity", 1))
                price = float(item.get("unit_price", 0))
            except (ValueError, TypeError) as exc:
                return ToolResult(
                    content=f"Error: invalid line item values: {exc}",
                    is_error=True,
                    error_kind=ToolErrorKind.VALIDATION,
                )

            if qty < 0 or price < 0:
                return ToolResult(
                    content="Error: quantity and unit_price must not be negative.",
                    is_error=True,
                    error_kind=ToolErrorKind.VALIDATION,
                )

            total = qty * price
            subtotal += total
            processed_items.append(
                {
                    "description": str(item.get("description", "")),
                    "quantity": qty,
                    "unit_price": price,
                    "total": total,
                }
            )

        total_amount = subtotal

        # Build client slug for folder organization
        client_slug = (
            make_client_slug(
                name=client_name or "",
                address=client_address or "",
                folder_scheme=user.folder_scheme,
            )
            or None
        )

        # Create invoice via store
        invoice_store = InvoiceStore(user.id)
        invoice = await invoice_store.create(
            description=description,
            total_amount=total_amount,
            status=InvoiceStatus.DRAFT,
            client_id=client_slug,
            line_items=processed_items,
            due_date=due_date,
            notes=notes or "",
        )

        invoice_number = invoice.id

        # Generate and save PDF
        try:
            await _generate_invoice_pdf_and_save(
                user=user,
                invoice_id=invoice_number,
                description=description,
                processed_items=processed_items,
                subtotal=subtotal,
                total_amount=total_amount,
                client_name=client_name,
                client_address=client_address,
                client_slug=client_slug,
                due_date=due_date,
                notes=notes,
                storage=storage,
                invoice_store=invoice_store,
            )
        except Exception:
            logger.exception("PDF generation failed for invoice %s", invoice_number)
            return ToolResult(
                content=(
                    f"Invoice {invoice_number} created but PDF generation failed. "
                    f"The invoice record exists and can be retried."
                ),
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

        return ToolResult(
            content=(
                f"Invoice {invoice_number} generated for ${total_amount:,.2f}. "
                f"{len(processed_items)} line item(s). "
                f"PDF saved. "
                f"Use send_media_reply to send it to the user."
            )
        )

    async def convert_estimate_to_invoice(
        estimate_id: str,
        due_date: str | None = None,
        notes: str | None = None,
    ) -> ToolResult:
        """Convert an accepted estimate to an invoice."""
        estimate_store = EstimateStore(user.id)
        estimate = await estimate_store.get(estimate_id)

        if not estimate:
            return ToolResult(
                content=f"Error: Estimate {estimate_id} not found.",
                is_error=True,
                error_kind=ToolErrorKind.NOT_FOUND,
            )

        if estimate.status != EstimateStatus.ACCEPTED:
            return ToolResult(
                content=(
                    f"Error: Estimate {estimate_id} has status '{estimate.status}'. "
                    f"Only accepted estimates can be converted to invoices."
                ),
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )

        # Build line items from estimate
        processed_items: list[dict[str, Any]] = [
            {
                "description": li.description,
                "quantity": li.quantity,
                "unit_price": li.unit_price,
                "total": li.total,
            }
            for li in estimate.line_items
        ]

        # Create invoice linked to estimate
        invoice_store = InvoiceStore(user.id)
        invoice = await invoice_store.create(
            description=estimate.description,
            total_amount=estimate.total_amount,
            status=InvoiceStatus.DRAFT,
            client_id=estimate.client_id or None,
            line_items=processed_items,
            due_date=due_date,
            estimate_id=estimate_id,
            notes=notes or "",
        )

        invoice_number = invoice.id
        client_slug = estimate.client_id or None

        # Resolve client name/address for PDF from client record
        client_name = None
        client_address = None
        if estimate.client_id:
            client_store = ClientStore(user.id)
            client_data = await client_store.get(estimate.client_id)
            if client_data:
                client_name = client_data.name or None
                client_address = client_data.address or None

        # Generate and save PDF
        try:
            await _generate_invoice_pdf_and_save(
                user=user,
                invoice_id=invoice_number,
                description=estimate.description,
                processed_items=processed_items,
                subtotal=estimate.total_amount,
                total_amount=estimate.total_amount,
                client_name=client_name,
                client_address=client_address,
                client_slug=client_slug,
                due_date=due_date,
                notes=notes,
                storage=storage,
                invoice_store=invoice_store,
            )
        except Exception:
            logger.exception("PDF generation failed for invoice %s", invoice_number)
            return ToolResult(
                content=(
                    f"Invoice {invoice_number} created from estimate {estimate_id} "
                    f"but PDF generation failed. The invoice record exists and can be retried."
                ),
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

        return ToolResult(
            content=(
                f"Invoice {invoice_number} created from estimate {estimate_id} "
                f"for ${estimate.total_amount:,.2f}. "
                f"{len(processed_items)} line item(s). "
                f"PDF saved. "
                f"Use send_media_reply to send it to the user."
            )
        )

    return [
        Tool(
            name=ToolName.GENERATE_INVOICE,
            description=(
                "Generate a professional invoice PDF. Use when the user asks for "
                "an invoice or bill. Requires line_items: each item needs a "
                "description, quantity, and unit_price. Do NOT call this tool until you "
                "have at least one concrete line item from the user."
            ),
            function=generate_invoice,
            params_model=GenerateInvoiceParams,
            usage_hint=(
                "Before calling this tool, ask the user for specific line items "
                "(what work, how much, at what price). Do not guess line items."
            ),
        ),
        Tool(
            name=ToolName.CONVERT_ESTIMATE_TO_INVOICE,
            description=(
                "Convert an accepted estimate into an invoice. The invoice will "
                "have the same line items and total as the estimate. The estimate "
                "must have status 'accepted' before conversion."
            ),
            function=convert_estimate_to_invoice,
            params_model=ConvertEstimateToInvoiceParams,
            usage_hint=(
                "Only call this when the user explicitly asks to convert an estimate "
                "to an invoice. The estimate must be accepted first."
            ),
        ),
    ]


def _invoice_factory(ctx: ToolContext) -> list[Tool]:
    """Factory for invoice tools, used by the registry."""
    return create_invoice_tools(ctx.user, ctx.storage)


def _register() -> None:
    from backend.app.agent.tools.registry import default_registry

    default_registry.register(
        "invoice",
        _invoice_factory,
        core=False,
        summary="Generate invoices with payment tracking and PDF output",
    )


_register()
