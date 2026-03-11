"""QuickBooks Online tools for the agent."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.services.quickbooks_service import QuickBooksService, get_quickbooks_service

if TYPE_CHECKING:
    from backend.app.agent.tools.registry import ToolContext

logger = logging.getLogger(__name__)


class QBSearchInvoicesParams(BaseModel):
    """Parameters for the qb_search_invoices tool."""

    customer_name: str | None = Field(
        default=None, description="Customer name to filter invoices by (optional)"
    )


class QBSearchEstimatesParams(BaseModel):
    """Parameters for the qb_search_estimates tool."""

    customer_name: str | None = Field(
        default=None, description="Customer name to filter estimates by (optional)"
    )


def create_quickbooks_tools(
    qb_service: QuickBooksService,
) -> list[Tool]:
    """Create QuickBooks-related tools for the agent."""

    async def qb_search_invoices(customer_name: str | None = None) -> ToolResult:
        """Search QuickBooks invoices, optionally filtered by customer name."""
        try:
            invoices = await qb_service.list_invoices(customer_name)
        except Exception as exc:
            logger.exception("QuickBooks list_invoices failed")
            return ToolResult(
                content=f"Error searching QuickBooks invoices: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

        if not invoices:
            label = f" for '{customer_name}'" if customer_name else ""
            return ToolResult(content=f"No invoices found{label}.")

        lines = [f"Found {len(invoices)} invoice(s):"]
        for inv in invoices:
            paid = float(inv.get("total", 0)) - float(inv.get("balance", 0))
            status = "Paid" if float(inv.get("balance", 0)) == 0 else "Open"
            lines.append(
                f"- #{inv.get('doc_number', 'N/A')} | {inv.get('customer_name', 'Unknown')}"
                f" | ${inv.get('total', 0):,.2f} ({status}, ${paid:,.2f} paid)"
                f" | Date: {inv.get('date', 'N/A')}"
                f" | Due: {inv.get('due_date', 'N/A')}"
            )
        return ToolResult(content="\n".join(lines))

    async def qb_search_estimates(customer_name: str | None = None) -> ToolResult:
        """Search QuickBooks estimates, optionally filtered by customer name."""
        try:
            estimates = await qb_service.list_estimates(customer_name)
        except Exception as exc:
            logger.exception("QuickBooks list_estimates failed")
            return ToolResult(
                content=f"Error searching QuickBooks estimates: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

        if not estimates:
            label = f" for '{customer_name}'" if customer_name else ""
            return ToolResult(content=f"No estimates found{label}.")

        lines = [f"Found {len(estimates)} estimate(s):"]
        for est in estimates:
            lines.append(
                f"- #{est.get('doc_number', 'N/A')} | {est.get('customer_name', 'Unknown')}"
                f" | ${est.get('total', 0):,.2f}"
                f" | Status: {est.get('status', 'N/A')}"
                f" | Date: {est.get('date', 'N/A')}"
                f" | Expires: {est.get('expiry_date', 'N/A')}"
            )
        return ToolResult(content="\n".join(lines))

    return [
        Tool(
            name=ToolName.QB_SEARCH_INVOICES,
            description=(
                "Search QuickBooks Online invoices. "
                "Optionally filter by customer name to see their invoice history."
            ),
            function=qb_search_invoices,
            params_model=QBSearchInvoicesParams,
            usage_hint="Search for invoices in QuickBooks, optionally by customer name.",
        ),
        Tool(
            name=ToolName.QB_SEARCH_ESTIMATES,
            description=(
                "Search QuickBooks Online estimates. "
                "Optionally filter by customer name to see their estimate history."
            ),
            function=qb_search_estimates,
            params_model=QBSearchEstimatesParams,
            usage_hint="Search for estimates in QuickBooks, optionally by customer name.",
        ),
    ]


def _quickbooks_factory(ctx: ToolContext) -> list[Tool]:
    """Factory for QuickBooks tools, used by the registry."""
    qb_service = get_quickbooks_service()
    if qb_service is None:
        return []
    return create_quickbooks_tools(qb_service)


def _register() -> None:
    from backend.app.agent.tools.registry import default_registry

    default_registry.register(
        "quickbooks",
        _quickbooks_factory,
        core=False,
        summary="View invoices and estimates from QuickBooks Online",
    )


_register()
