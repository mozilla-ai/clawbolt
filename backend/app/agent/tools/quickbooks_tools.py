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


def _make_token_refresh_callback(user_id: int, realm_id: str) -> Any:
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
    ]


def _get_quickbooks_service_for_user(user_id: int) -> QuickBooksService | None:
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
        summary="Query QuickBooks Online for invoices, estimates, customers, items, and more",
    )


_register()
