"""Estimate generation tools for the agent."""

from __future__ import annotations

import datetime
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from backend.app.agent.file_store import ContractorData, EstimateStore, make_client_slug
from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolResult
from backend.app.agent.tools.file_tools import build_folder_path
from backend.app.agent.tools.names import ToolName
from backend.app.config import settings
from backend.app.enums import EstimateStatus
from backend.app.services.pdf_service import EstimatePDFData, generate_estimate_pdf
from backend.app.services.storage_service import StorageBackend

if TYPE_CHECKING:
    from backend.app.agent.tools.registry import ToolContext

PDF_BASE_DIR = Path(settings.pdf_storage_dir)

logger = logging.getLogger(__name__)


class EstimateLineItemParams(BaseModel):
    """A single line item in an estimate."""

    description: str = Field(description="Description of the line item")
    quantity: float = Field(default=1, ge=0, description="Quantity")
    unit_price: float = Field(ge=0, description="Price per unit")


class GenerateEstimateParams(BaseModel):
    """Parameters for the generate_estimate tool."""

    description: str = Field(description="Brief description of the work")
    line_items: list[EstimateLineItemParams] = Field(
        description="Line items for the estimate",
    )
    client_name: str | None = Field(default=None, description="Client name (optional)")
    client_address: str | None = Field(default=None, description="Client address (optional)")
    terms: str | None = Field(default=None, description="Payment terms (optional)")


def create_estimate_tools(
    contractor: ContractorData,
    storage: StorageBackend | None = None,
) -> list[Tool]:
    """Create estimate-related tools for the agent."""

    async def generate_estimate(
        description: str,
        line_items: list[dict[str, Any]],
        client_name: str | None = None,
        client_address: str | None = None,
        terms: str | None = None,
    ) -> ToolResult:
        """Generate a professional estimate PDF and return summary."""
        today = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")

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
                folder_scheme=contractor.folder_scheme,
            )
            or None
        )

        # Create estimate via file store
        estimate_store = EstimateStore(contractor.id)
        estimate = await estimate_store.create(
            description=description,
            total_amount=total_amount,
            status=EstimateStatus.DRAFT,
            client_id=client_slug,
            line_items=processed_items,
        )

        estimate_number = estimate.id  # Already in EST-NNNN format

        # Generate PDF
        pdf_data = EstimatePDFData(
            contractor_name=contractor.name or "Contractor",
            contractor_phone=contractor.phone or "",
            contractor_trade="",
            description=description,
            line_items=processed_items,
            subtotal=subtotal,
            total=total_amount,
            estimate_date=today,
            estimate_number=estimate_number,
            client_name=client_name,
            client_address=client_address,
            terms=terms,
        )

        pdf_bytes = await generate_estimate_pdf(pdf_data)

        # Save PDF to local storage, organized by client
        pdf_dir = PDF_BASE_DIR / str(contractor.id) / (client_slug or "unsorted")
        pdf_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = pdf_dir / f"{estimate.id}.pdf"
        pdf_path.write_bytes(pdf_bytes)

        # Also upload to cloud storage if available
        cloud_path = ""
        if storage:
            try:
                folder_path = build_folder_path("estimate", client_name, client_address)
                await storage.create_folder(folder_path)
                await storage.upload_file(pdf_bytes, folder_path, f"{estimate_number}.pdf")
                cloud_path = f"{folder_path}/{estimate_number}.pdf"
            except Exception:
                logger.warning(
                    "Cloud upload failed for estimate %s, local PDF saved successfully",
                    estimate_number,
                )

        # Update estimate with PDF path and cloud storage path
        update_fields: dict[str, str] = {"pdf_url": str(pdf_path)}
        if cloud_path:
            update_fields["storage_path"] = cloud_path
        await estimate_store.update(estimate.id, **update_fields)

        return ToolResult(
            content=(
                f"Estimate {estimate_number} generated for ${total_amount:,.2f}. "
                f"{len(processed_items)} line item(s). "
                f"PDF saved at {pdf_path}. "
                f"Use send_media_reply to send it to the contractor."
            )
        )

    return [
        Tool(
            name=ToolName.GENERATE_ESTIMATE,
            description=(
                "Generate a professional estimate PDF. Use when the contractor asks for "
                "an estimate, quote, or bid. Requires line_items: each item needs a "
                "description, quantity, and unit_price. Do NOT call this tool until you "
                "have at least one concrete line item from the contractor."
            ),
            function=generate_estimate,
            params_model=GenerateEstimateParams,
            usage_hint=(
                "Before calling this tool, ask the contractor for specific line items "
                "(what work, how much, at what price). Do not guess line items."
            ),
        ),
    ]


def _estimate_factory(ctx: ToolContext) -> list[Tool]:
    """Factory for estimate tools, used by the registry."""
    return create_estimate_tools(ctx.contractor, ctx.storage)


def _register() -> None:
    from backend.app.agent.tools.registry import default_registry

    default_registry.register(
        "estimate",
        _estimate_factory,
        core=False,
        summary="Generate professional estimates and quotes with PDF output",
    )


_register()
