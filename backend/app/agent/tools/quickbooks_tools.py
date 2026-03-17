"""QuickBooks Online tools for the agent."""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.config import settings
from backend.app.services.oauth import (
    OAuthTokenData,
    oauth_service,
)
from backend.app.services.quickbooks_service import (
    QuickBooksOnlineService,
    QuickBooksService,
)

if TYPE_CHECKING:
    from backend.app.agent.tools.registry import ToolContext

logger = logging.getLogger(__name__)

# Maximum number of rows to include in the tool response to keep context lean.
_MAX_ROWS = 50


class QBQueryParams(BaseModel):
    """Parameters for the qb_query tool."""

    query: str = Field(
        description="A QBO query string (SELECT only). Example: SELECT * FROM Invoice MAXRESULTS 20"
    )


class QBLineItem(BaseModel):
    """A single line item for a QB estimate or invoice."""

    description: str = Field(description="Line item description")
    quantity: float = Field(default=1.0, description="Quantity")
    unit_price: float = Field(description="Unit price in dollars")


class QBCreateEstimateParams(BaseModel):
    """Parameters for qb_create_estimate."""

    customer_name: str = Field(description="Customer display name (must exist in QB)")
    line_items: list[QBLineItem] = Field(description="Line items for the estimate")
    expiration_date: str | None = Field(
        default=None, description="Expiration date in YYYY-MM-DD format"
    )
    memo: str | None = Field(default=None, description="Customer memo / notes")


class QBCreateInvoiceParams(BaseModel):
    """Parameters for qb_create_invoice."""

    customer_name: str = Field(description="Customer display name (must exist in QB)")
    line_items: list[QBLineItem] = Field(description="Line items for the invoice")
    due_date: str | None = Field(default=None, description="Due date in YYYY-MM-DD format")
    memo: str | None = Field(default=None, description="Customer memo / notes")


class QBCreateCustomerParams(BaseModel):
    """Parameters for qb_create_customer."""

    display_name: str = Field(description="Customer display name (must be unique in QB)")
    email: str | None = Field(default=None, description="Customer email address")
    phone: str | None = Field(default=None, description="Customer phone number")


class QBSendInvoiceParams(BaseModel):
    """Parameters for qb_send_invoice."""

    invoice_id: str = Field(description="QuickBooks Invoice ID")
    email: str = Field(description="Email address to send the invoice to")


class QBEstimateToInvoiceParams(BaseModel):
    """Parameters for qb_estimate_to_invoice."""

    estimate_id: str = Field(description="QuickBooks Estimate ID to convert")


def _format_results(rows: list[dict[str, Any]]) -> str:
    """Format QBO query results into a readable string for the LLM."""
    if not rows:
        return "Query returned 0 results."

    truncated = rows[:_MAX_ROWS]
    lines = [f"Query returned {len(rows)} result(s):"]
    for row in truncated:
        # Build a compact key: value summary, skipping deeply nested metadata
        parts: list[str] = []
        for key, val in row.items():
            if key in ("domain", "sparse", "SyncToken", "MetaData"):
                continue
            if isinstance(val, dict):
                # Ref fields like CustomerRef: show name + value
                name = val.get("name", "")
                ref_val = val.get("value", "")
                if name:
                    parts.append(f"{key}: {name} ({ref_val})")
                elif ref_val:
                    parts.append(f"{key}: {ref_val}")
            elif isinstance(val, list):
                parts.append(f"{key}: [{len(val)} items]")
            else:
                parts.append(f"{key}: {val}")
        lines.append("- " + " | ".join(parts))

    if len(rows) > _MAX_ROWS:
        lines.append(f"... and {len(rows) - _MAX_ROWS} more (add MAXRESULTS to narrow)")

    return "\n".join(lines)


def _make_token_refresh_callback(user_id: str, realm_id: str) -> Any:
    """Return a callback that persists refreshed tokens to disk."""

    def _persist_refreshed_tokens(access_token: str, refresh_token: str) -> None:
        token = oauth_service.load_token(user_id, "quickbooks")
        if token is None:
            token = OAuthTokenData(
                access_token=access_token,
                refresh_token=refresh_token,
                realm_id=realm_id,
            )
        else:
            token.access_token = access_token
            token.refresh_token = refresh_token
            # QBO access tokens last 1 hour
            token.expires_at = time.time() + 3600
        oauth_service.save_token(user_id, "quickbooks", token)

    return _persist_refreshed_tokens


async def _lookup_customer_id(
    qb_service: QuickBooksService, customer_name: str
) -> tuple[str | None, str | None]:
    """Look up a customer by name. Returns (customer_id, error_message)."""
    try:
        escaped = customer_name.replace("'", "\\'")
        rows = await qb_service.query(
            f"SELECT Id, DisplayName FROM Customer WHERE DisplayName = '{escaped}'"
        )
    except Exception as exc:
        return None, f"Customer lookup failed: {exc}"
    if not rows:
        return None, f"Customer '{customer_name}' not found in QuickBooks. Create them first."
    return rows[0].get("Id"), None


def _build_qb_line_items(items: list[QBLineItem | dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert QBLineItem params to QBO API line item format."""
    lines: list[dict[str, Any]] = []
    for i, raw in enumerate(items, 1):
        item = raw if isinstance(raw, QBLineItem) else QBLineItem(**raw)
        lines.append(
            {
                "Id": str(i),
                "LineNum": i,
                "Amount": round(item.quantity * item.unit_price, 2),
                "DetailType": "SalesItemLineDetail",
                "Description": item.description,
                "SalesItemLineDetail": {
                    "Qty": item.quantity,
                    "UnitPrice": item.unit_price,
                },
            }
        )
    return lines


def create_quickbooks_tools(
    qb_service: QuickBooksService,
) -> list[Tool]:
    """Create QuickBooks-related tools for the agent."""

    async def qb_query(query: str) -> ToolResult:
        """Run a read-only query against QuickBooks Online."""
        normalized = query.strip()
        if not normalized.upper().startswith("SELECT"):
            return ToolResult(
                content="Only SELECT queries are supported.",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )

        try:
            rows = await qb_service.query(normalized)
        except Exception as exc:
            logger.exception("QuickBooks query failed")
            error_str = str(exc)
            # Include the response body for QBO 400 errors so the LLM can self-correct
            if hasattr(exc, "response"):
                try:
                    body = exc.response.json()  # type: ignore[union-attr]
                    error_str = json.dumps(body, indent=2)
                except Exception:
                    pass
            return ToolResult(
                content=f"QuickBooks query error:\n{error_str}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

        return ToolResult(content=_format_results(rows))

    async def qb_create_estimate(
        customer_name: str,
        line_items: list[QBLineItem | dict[str, Any]],
        expiration_date: str | None = None,
        memo: str | None = None,
    ) -> ToolResult:
        """Create an estimate in QuickBooks Online."""
        customer_id, err = await _lookup_customer_id(qb_service, customer_name)
        if err:
            return ToolResult(content=err, is_error=True, error_kind=ToolErrorKind.NOT_FOUND)

        body: dict[str, Any] = {
            "CustomerRef": {"value": customer_id},
            "Line": _build_qb_line_items(line_items),
        }
        if expiration_date:
            body["ExpirationDate"] = expiration_date
        if memo:
            body["CustomerMemo"] = {"value": memo}

        try:
            result = await qb_service.create_entity("Estimate", body)
        except Exception as exc:
            logger.exception("QB create estimate failed")
            return ToolResult(
                content=f"Failed to create estimate: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

        doc_num = result.get("DocNumber", "?")
        total = result.get("TotalAmt", 0)
        return ToolResult(
            content=(
                f"Estimate created in QuickBooks.\n"
                f"DocNumber: {doc_num} | Total: ${total:.2f} | "
                f"Customer: {customer_name}"
            )
        )

    async def qb_create_invoice(
        customer_name: str,
        line_items: list[QBLineItem | dict[str, Any]],
        due_date: str | None = None,
        memo: str | None = None,
    ) -> ToolResult:
        """Create an invoice in QuickBooks Online."""
        customer_id, err = await _lookup_customer_id(qb_service, customer_name)
        if err:
            return ToolResult(content=err, is_error=True, error_kind=ToolErrorKind.NOT_FOUND)

        body: dict[str, Any] = {
            "CustomerRef": {"value": customer_id},
            "Line": _build_qb_line_items(line_items),
        }
        if due_date:
            body["DueDate"] = due_date
        if memo:
            body["CustomerMemo"] = {"value": memo}

        try:
            result = await qb_service.create_entity("Invoice", body)
        except Exception as exc:
            logger.exception("QB create invoice failed")
            return ToolResult(
                content=f"Failed to create invoice: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

        doc_num = result.get("DocNumber", "?")
        total = result.get("TotalAmt", 0)
        inv_id = result.get("Id", "?")
        return ToolResult(
            content=(
                f"Invoice created in QuickBooks.\n"
                f"Id: {inv_id} | DocNumber: {doc_num} | Total: ${total:.2f} | "
                f"Customer: {customer_name}"
            )
        )

    async def qb_create_customer(
        display_name: str,
        email: str | None = None,
        phone: str | None = None,
    ) -> ToolResult:
        """Create a customer in QuickBooks Online."""
        body: dict[str, Any] = {"DisplayName": display_name}
        if email:
            body["PrimaryEmailAddr"] = {"Address": email}
        if phone:
            body["PrimaryPhone"] = {"FreeFormNumber": phone}

        try:
            result = await qb_service.create_entity("Customer", body)
        except Exception as exc:
            logger.exception("QB create customer failed")
            error_str = str(exc)
            if hasattr(exc, "response"):
                try:
                    error_body = exc.response.json()  # type: ignore[union-attr]
                    error_str = json.dumps(error_body, indent=2)
                except Exception:
                    pass
            return ToolResult(
                content=f"Failed to create customer: {error_str}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

        cust_id = result.get("Id", "?")
        return ToolResult(
            content=(f"Customer created in QuickBooks.\nId: {cust_id} | Name: {display_name}")
        )

    async def qb_send_invoice(invoice_id: str, email: str) -> ToolResult:
        """Send an invoice via QuickBooks email."""
        try:
            await qb_service.send_invoice_email(invoice_id, email)
        except Exception as exc:
            logger.exception("QB send invoice email failed")
            return ToolResult(
                content=f"Failed to send invoice: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

        return ToolResult(content=f"Invoice {invoice_id} sent to {email} via QuickBooks.")

    async def qb_estimate_to_invoice(estimate_id: str) -> ToolResult:
        """Convert a QuickBooks estimate to an invoice."""
        # Load the estimate to get its details
        try:
            rows = await qb_service.query(f"SELECT * FROM Estimate WHERE Id = '{estimate_id}'")
        except Exception as exc:
            return ToolResult(
                content=f"Failed to load estimate: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

        if not rows:
            return ToolResult(
                content=f"Estimate {estimate_id} not found in QuickBooks.",
                is_error=True,
                error_kind=ToolErrorKind.NOT_FOUND,
            )

        estimate = rows[0]
        customer_ref = estimate.get("CustomerRef", {})

        # Build invoice with LinkedTxn referencing the estimate
        invoice_body: dict[str, Any] = {
            "CustomerRef": customer_ref,
            "Line": estimate.get("Line", []),
            "LinkedTxn": [
                {
                    "TxnId": estimate_id,
                    "TxnType": "Estimate",
                }
            ],
        }
        if estimate.get("CustomerMemo"):
            invoice_body["CustomerMemo"] = estimate["CustomerMemo"]

        try:
            result = await qb_service.create_entity("Invoice", invoice_body)
        except Exception as exc:
            logger.exception("QB estimate-to-invoice failed")
            return ToolResult(
                content=f"Failed to create invoice from estimate: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

        doc_num = result.get("DocNumber", "?")
        total = result.get("TotalAmt", 0)
        inv_id = result.get("Id", "?")
        customer_name = customer_ref.get("name", "?")
        return ToolResult(
            content=(
                f"Invoice created from Estimate {estimate_id}.\n"
                f"Invoice Id: {inv_id} | DocNumber: {doc_num} | "
                f"Total: ${total:.2f} | Customer: {customer_name}"
            )
        )

    return [
        Tool(
            name=ToolName.QB_QUERY,
            description=(
                "Run a read-only query against QuickBooks Online using QBO query language "
                "(SQL-like SELECT statements). Use this to look up invoices, estimates, "
                "customers, items, payments, and more.\n\n"
                "Common entities and useful fields:\n"
                "- Invoice: Id, DocNumber, CustomerRef, TotalAmt, Balance, DueDate, TxnDate, EmailStatus\n"
                "- Estimate: Id, DocNumber, CustomerRef, TotalAmt, TxnDate, ExpirationDate, TxnStatus\n"
                "- Customer: Id, DisplayName, PrimaryEmailAddr, PrimaryPhone, Balance\n"
                "- Item: Id, Name, Description, UnitPrice, Type\n"
                "- Payment: Id, CustomerRef, TotalAmt, TxnDate\n"
                "- Bill: Id, VendorRef, TotalAmt, DueDate, Balance\n\n"
                "Syntax: SELECT <fields> FROM <Entity> [WHERE <conditions>] "
                "[ORDERBY <field> DESC] [MAXRESULTS <n>]\n"
                "Operators: =, <, >, <=, >=, LIKE '%text%', IN ('a','b')\n"
                "Note: No subqueries. To filter by customer name, first query Customer "
                "to get the Id, then use CustomerRef = '<id>' in a second query."
            ),
            function=qb_query,
            params_model=QBQueryParams,
            usage_hint=(
                "Query QuickBooks for invoices, estimates, customers, items, and more. "
                "Use SELECT ... FROM <Entity> syntax."
            ),
        ),
        Tool(
            name=ToolName.QB_CREATE_ESTIMATE,
            description=(
                "Create an estimate in QuickBooks Online. The customer must already exist. "
                "Provide line items with description, quantity, and unit price."
            ),
            function=qb_create_estimate,
            params_model=QBCreateEstimateParams,
            usage_hint="Create a QB estimate. Look up the customer first if needed.",
        ),
        Tool(
            name=ToolName.QB_CREATE_INVOICE,
            description=(
                "Create an invoice in QuickBooks Online. The customer must already exist. "
                "Provide line items with description, quantity, and unit price."
            ),
            function=qb_create_invoice,
            params_model=QBCreateInvoiceParams,
            usage_hint="Create a QB invoice. Look up the customer first if needed.",
        ),
        Tool(
            name=ToolName.QB_CREATE_CUSTOMER,
            description=(
                "Create a new customer in QuickBooks Online. The display name must be unique."
            ),
            function=qb_create_customer,
            params_model=QBCreateCustomerParams,
            usage_hint="Create a customer in QB before creating estimates or invoices for them.",
        ),
        Tool(
            name=ToolName.QB_SEND_INVOICE,
            description=(
                "Send an invoice to a customer via QuickBooks email. "
                "The invoice must already exist in QuickBooks."
            ),
            function=qb_send_invoice,
            params_model=QBSendInvoiceParams,
            usage_hint="Send a QB invoice by email. Confirm the email address first.",
        ),
        Tool(
            name=ToolName.QB_ESTIMATE_TO_INVOICE,
            description=(
                "Convert a QuickBooks estimate into an invoice. "
                "Creates a new invoice linked to the original estimate."
            ),
            function=qb_estimate_to_invoice,
            params_model=QBEstimateToInvoiceParams,
            usage_hint="Convert an existing QB estimate to an invoice.",
        ),
    ]


def _get_quickbooks_service_for_user(user_id: str) -> QuickBooksService | None:
    """Build a QuickBooks service using OAuth tokens for the given user."""
    token = oauth_service.load_token(user_id, "quickbooks")
    if token and token.access_token and token.realm_id:
        return QuickBooksOnlineService(
            client_id=settings.quickbooks_client_id,
            client_secret=settings.quickbooks_client_secret,
            realm_id=token.realm_id,
            access_token=token.access_token,
            refresh_token=token.refresh_token,
            environment=settings.quickbooks_environment,
            on_token_refresh=_make_token_refresh_callback(user_id, token.realm_id),
        )
    return None


def _quickbooks_factory(ctx: ToolContext) -> list[Tool]:
    """Factory for QuickBooks tools, used by the registry."""
    if not settings.quickbooks_client_id or not settings.quickbooks_client_secret:
        return []
    qb_service = _get_quickbooks_service_for_user(ctx.user.id)
    if qb_service is None:
        return []
    return create_quickbooks_tools(qb_service)


def _register() -> None:
    from backend.app.agent.tools.registry import default_registry

    default_registry.register(
        "quickbooks",
        _quickbooks_factory,
        core=False,
        summary=(
            "Query, create, and manage QuickBooks Online entities: "
            "invoices, estimates, customers, and more"
        ),
    )


_register()
