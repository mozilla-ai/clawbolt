"""QuickBooks Online tools for the agent."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.services.quickbooks_service import QuickBooksService, get_quickbooks_service

if TYPE_CHECKING:
    from backend.app.agent.tools.registry import ToolContext

logger = logging.getLogger(__name__)


class QBSearchItemsParams(BaseModel):
    """Parameters for the qb_search_items tool."""

    query: str = Field(description="Search term to find items/services by name")


class QBSearchCustomersParams(BaseModel):
    """Parameters for the qb_search_customers tool."""

    query: str = Field(description="Search term to find customers by display name")


class QBCreateInvoiceLineItem(BaseModel):
    """A single line item for a QuickBooks invoice."""

    description: str = Field(description="Description of the line item")
    quantity: float = Field(default=1, ge=0, description="Quantity")
    unit_price: float = Field(ge=0, description="Price per unit")
    item_id: str | None = Field(default=None, description="QBO item ID (optional)")


class QBCreateInvoiceParams(BaseModel):
    """Parameters for the qb_create_invoice tool."""

    customer_id: str = Field(description="QuickBooks customer ID")
    line_items: list[QBCreateInvoiceLineItem] = Field(
        description="Line items for the invoice",
    )


class QBSendInvoiceParams(BaseModel):
    """Parameters for the qb_send_invoice tool."""

    invoice_id: str = Field(description="QuickBooks invoice ID to send via email")


def create_quickbooks_tools(
    qb_service: QuickBooksService,
) -> list[Tool]:
    """Create QuickBooks-related tools for the agent."""

    async def qb_search_items(query: str) -> ToolResult:
        """Search QuickBooks items/services for pricing info."""
        try:
            items = await qb_service.list_items(query)
        except Exception as exc:
            logger.exception("QuickBooks list_items failed")
            return ToolResult(
                content=f"Error searching QuickBooks items: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

        if not items:
            return ToolResult(content=f"No items found matching '{query}'.")

        lines = [f"Found {len(items)} item(s):"]
        for item in items:
            price = f"${item.get('unit_price', 0):,.2f}" if item.get("unit_price") else "N/A"
            lines.append(
                f"- {item['name']} (ID: {item['id']}, Price: {price})"
                f" | {item.get('description', '')}"
            )
        return ToolResult(content="\n".join(lines))

    async def qb_search_customers(query: str) -> ToolResult:
        """Search QuickBooks customers."""
        try:
            customers = await qb_service.list_customers(query)
        except Exception as exc:
            logger.exception("QuickBooks list_customers failed")
            return ToolResult(
                content=f"Error searching QuickBooks customers: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

        if not customers:
            return ToolResult(content=f"No customers found matching '{query}'.")

        lines = [f"Found {len(customers)} customer(s):"]
        for cust in customers:
            email = cust.get("primary_email", "")
            phone = cust.get("primary_phone", "")
            contact = f"Email: {email}" if email else ""
            if phone:
                contact = f"{contact}, Phone: {phone}" if contact else f"Phone: {phone}"
            lines.append(f"- {cust['display_name']} (ID: {cust['id']}) | {contact}")
        return ToolResult(content="\n".join(lines))

    async def qb_create_invoice(
        customer_id: str,
        line_items: list[dict[str, Any]],
    ) -> ToolResult:
        """Create a QuickBooks invoice."""
        if not line_items:
            return ToolResult(
                content="Error: at least one line item is required.",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )

        # Build line items with computed amounts
        processed: list[dict[str, Any]] = []
        for item in line_items:
            qty = float(item.get("quantity", 1))
            price = float(item.get("unit_price", 0))
            processed.append(
                {
                    "description": item.get("description", ""),
                    "quantity": qty,
                    "unit_price": price,
                    "amount": qty * price,
                    "item_id": item.get("item_id"),
                }
            )

        try:
            invoice = await qb_service.create_invoice(customer_id, processed)
        except Exception as exc:
            logger.exception("QuickBooks create_invoice failed")
            return ToolResult(
                content=f"Error creating QuickBooks invoice: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

        return ToolResult(
            content=(
                f"Invoice created successfully. "
                f"Invoice ID: {invoice['id']}, "
                f"Doc Number: {invoice.get('doc_number', 'N/A')}, "
                f"Total: ${invoice.get('total', 0):,.2f}. "
                f"Use qb_send_invoice to email it to the customer."
            )
        )

    async def qb_send_invoice(invoice_id: str) -> ToolResult:
        """Email a QuickBooks invoice to the customer."""
        try:
            result = await qb_service.send_invoice(invoice_id)
        except Exception as exc:
            logger.exception("QuickBooks send_invoice failed")
            return ToolResult(
                content=f"Error sending QuickBooks invoice: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

        return ToolResult(
            content=(f"Invoice {result.get('id', invoice_id)} sent successfully via email.")
        )

    return [
        Tool(
            name=ToolName.QB_SEARCH_ITEMS,
            description=(
                "Search QuickBooks Online items and services for pricing information. "
                "Use when the contractor asks about pricing from their QuickBooks catalog."
            ),
            function=qb_search_items,
            params_model=QBSearchItemsParams,
            usage_hint="Search for items in QuickBooks to look up pricing.",
        ),
        Tool(
            name=ToolName.QB_SEARCH_CUSTOMERS,
            description=(
                "Search QuickBooks Online customers by name. "
                "Use to find customer IDs needed for creating invoices."
            ),
            function=qb_search_customers,
            params_model=QBSearchCustomersParams,
            usage_hint="Search for customers in QuickBooks by name.",
        ),
        Tool(
            name=ToolName.QB_CREATE_INVOICE,
            description=(
                "Create an invoice in QuickBooks Online. Requires a customer ID "
                "(use qb_search_customers to find it) and line items with description, "
                "quantity, and unit_price."
            ),
            function=qb_create_invoice,
            params_model=QBCreateInvoiceParams,
            usage_hint="Create an invoice in QuickBooks with customer ID and line items.",
        ),
        Tool(
            name=ToolName.QB_SEND_INVOICE,
            description=(
                "Email a QuickBooks invoice to the customer. "
                "Use after creating an invoice with qb_create_invoice."
            ),
            function=qb_send_invoice,
            params_model=QBSendInvoiceParams,
            usage_hint="Send a QuickBooks invoice via email after creating it.",
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
        summary="Look up pricing and customers, create and send invoices via QuickBooks Online",
    )


_register()
