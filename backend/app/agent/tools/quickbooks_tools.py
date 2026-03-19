"""QuickBooks Online tools for the agent."""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from backend.app.agent.approval import ApprovalPolicy, PermissionLevel
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

# Entities allowed in qb_query to prevent exfiltration of sensitive data.
_QUERYABLE_ENTITIES = {
    "INVOICE",
    "ESTIMATE",
    "CUSTOMER",
    "ITEM",
    "PAYMENT",
    "BILL",
    "VENDOR",
    "SALESRECEIPT",
    "CREDITMEMO",
    "PURCHASEORDER",
    "TIMEACTIVITY",
    "DEPOSIT",
    "TRANSFER",
    "JOURNALENTRY",
}

# Entity types that qb_create is allowed to create.
_CREATABLE_ENTITIES = {"Customer", "Estimate", "Invoice"}

# Entity types that qb_update is allowed to update.
_UPDATABLE_ENTITIES = {"Customer", "Estimate", "Invoice"}

# Entity types that qb_send is allowed to send via email.
_SENDABLE_ENTITIES = {"Invoice", "Estimate"}


class QBQueryParams(BaseModel):
    """Parameters for the qb_query tool."""

    query: str = Field(
        description=(
            "A QBO query string (SELECT only). Example: SELECT * FROM Invoice MAXRESULTS 20"
        )
    )


class QBCreateParams(BaseModel):
    """Parameters for the qb_create tool."""

    entity_type: str = Field(
        description="QBO entity type to create: Customer, Estimate, or Invoice"
    )
    data: dict[str, Any] = Field(
        description="QBO API payload for the entity. See SKILL.md for payload formats."
    )


class QBUpdateParams(BaseModel):
    """Parameters for the qb_update tool."""

    entity_type: str = Field(
        description="QBO entity type to update: Customer, Estimate, or Invoice"
    )
    data: dict[str, Any] = Field(
        description=(
            "Full QBO API payload including Id and SyncToken from a prior qb_query. "
            "See SKILL.md for payload formats."
        )
    )


class QBSendParams(BaseModel):
    """Parameters for the qb_send tool."""

    entity_type: str = Field(
        description="QBO entity type to send: Invoice or Estimate",
        default="Invoice",
    )
    entity_id: str = Field(description="QuickBooks entity ID (numeric)")
    email: str = Field(
        description="Email address to send to",
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    )


def _format_results(rows: list[dict[str, Any]]) -> str:
    """Format QBO query results into a readable string for the LLM."""
    if not rows:
        return "Query returned 0 results."

    truncated = rows[:_MAX_ROWS]
    lines = [f"Query returned {len(rows)} result(s):"]
    for row in truncated:
        parts: list[str] = []
        for key, val in row.items():
            if key in ("domain", "sparse", "MetaData"):
                continue
            if isinstance(val, dict):
                name = val.get("name", "")
                ref_val = val.get("value", "")
                if name:
                    parts.append(f"{key}: {name} ({ref_val})")
                elif ref_val:
                    parts.append(f"{key}: {ref_val}")
            elif isinstance(val, list):
                if key == "Line" and val:
                    items = []
                    for item in val:
                        if not isinstance(item, dict):
                            continue
                        desc = item.get("Description", "")
                        amt = item.get("Amount")
                        entry = f"{desc} ${amt:,.2f}" if amt is not None and desc else str(amt)
                        items.append(entry)
                    parts.append(f"Line: [{'; '.join(items)}]")
                else:
                    parts.append(f"{key}: {json.dumps(val)}")
            else:
                parts.append(f"{key}: {val}")
        lines.append("- " + " | ".join(parts))

    if len(rows) > _MAX_ROWS:
        lines.append(f"... and {len(rows) - _MAX_ROWS} more (add MAXRESULTS to narrow)")

    return "\n".join(lines)


def _make_token_refresh_callback(user_id: str, realm_id: str) -> Any:
    """Return a callback that persists refreshed tokens to disk."""

    def _persist_refreshed_tokens(access_token: str, refresh_token: str) -> None:
        try:
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
                token.expires_at = time.time() + 3600
            oauth_service.save_token(user_id, "quickbooks", token)
        except Exception:
            logger.exception("Failed to persist refreshed QuickBooks tokens for user %s", user_id)

    return _persist_refreshed_tokens


def create_quickbooks_tools(
    qb_service: QuickBooksService,
) -> list[Tool]:
    """Create QuickBooks-related tools for the agent."""

    async def qb_query(query: str) -> ToolResult:
        """Run a read-only query against QuickBooks Online."""
        import re as _re

        normalized = query.strip()
        if not normalized.upper().startswith("SELECT"):
            return ToolResult(
                content="Only SELECT queries are supported.",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )

        entity_match = _re.search(r"\bFROM\s+(\w+)", normalized, _re.IGNORECASE)
        if not entity_match:
            return ToolResult(
                content="Query must include a FROM clause (e.g. SELECT * FROM Invoice).",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )
        if entity_match.group(1).upper() not in _QUERYABLE_ENTITIES:
            return ToolResult(
                content=f"Querying '{entity_match.group(1)}' is not allowed. "
                f"Allowed entities: {', '.join(sorted(_QUERYABLE_ENTITIES))}",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )

        try:
            rows = await qb_service.query(normalized)
        except Exception as exc:
            logger.exception("QuickBooks query failed")
            error_str = str(exc)
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

    async def qb_create(entity_type: str, data: dict[str, Any]) -> ToolResult:
        """Create an entity in QuickBooks Online."""
        if entity_type not in _CREATABLE_ENTITIES:
            return ToolResult(
                content=f"Creating '{entity_type}' is not allowed. "
                f"Allowed: {', '.join(sorted(_CREATABLE_ENTITIES))}",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )

        try:
            result = await qb_service.create_entity(entity_type, data)
        except Exception as exc:
            logger.exception("QB create %s failed", entity_type)
            error_str = str(exc)
            if hasattr(exc, "response"):
                try:
                    error_body = exc.response.json()  # type: ignore[union-attr]
                    error_str = json.dumps(error_body, indent=2)
                except Exception:
                    pass
            return ToolResult(
                content=f"Failed to create {entity_type}: {error_str}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

        entity_id = result.get("Id", "?")
        doc_num = result.get("DocNumber", "")
        total = result.get("TotalAmt")
        display_name = result.get("DisplayName", "")

        parts = [f"{entity_type} created in QuickBooks.", f"Id: {entity_id}"]
        if doc_num:
            parts.append(f"DocNumber: {doc_num}")
        if total is not None:
            parts.append(f"Total: ${total:.2f}")
        if display_name:
            parts.append(f"Name: {display_name}")

        return ToolResult(content=" | ".join(parts))

    async def qb_update(entity_type: str, data: dict[str, Any]) -> ToolResult:
        """Update an existing entity in QuickBooks Online."""
        if entity_type not in _UPDATABLE_ENTITIES:
            return ToolResult(
                content=f"Updating '{entity_type}' is not allowed. "
                f"Allowed: {', '.join(sorted(_UPDATABLE_ENTITIES))}",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )

        try:
            result = await qb_service.update_entity(entity_type, data)
        except Exception as exc:
            logger.exception("QB update %s failed", entity_type)
            error_str = str(exc)
            if hasattr(exc, "response"):
                try:
                    error_body = exc.response.json()  # type: ignore[union-attr]
                    error_str = json.dumps(error_body, indent=2)
                except Exception:
                    pass
            return ToolResult(
                content=f"Failed to update {entity_type}: {error_str}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

        entity_id = result.get("Id", "?")
        doc_num = result.get("DocNumber", "")
        total = result.get("TotalAmt")
        display_name = result.get("DisplayName", "")

        parts = [f"{entity_type} updated in QuickBooks.", f"Id: {entity_id}"]
        if doc_num:
            parts.append(f"DocNumber: {doc_num}")
        if total is not None:
            parts.append(f"Total: ${total:.2f}")
        if display_name:
            parts.append(f"Name: {display_name}")

        return ToolResult(content=" | ".join(parts))

    async def qb_send(entity_type: str, entity_id: str, email: str) -> ToolResult:
        """Send an invoice or estimate via QuickBooks email."""
        if entity_type not in _SENDABLE_ENTITIES:
            return ToolResult(
                content=f"Sending '{entity_type}' is not allowed. "
                f"Allowed: {', '.join(sorted(_SENDABLE_ENTITIES))}",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )

        try:
            await qb_service.send_entity_email(entity_type, entity_id, email)
        except Exception as exc:
            logger.exception("QB send %s email failed", entity_type)
            return ToolResult(
                content=f"Failed to send {entity_type.lower()}: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

        return ToolResult(content=f"{entity_type} {entity_id} sent to {email} via QuickBooks.")

    return [
        Tool(
            name=ToolName.QB_QUERY,
            description=(
                "Run a read-only query against QuickBooks Online using QBO query language "
                "(SQL-like SELECT statements). Use this to look up invoices, estimates, "
                "customers, items, payments, and more. See the QuickBooks skill for "
                "query syntax and available entities."
            ),
            function=qb_query,
            params_model=QBQueryParams,
            usage_hint=(
                "Query QuickBooks for invoices, estimates, customers, items, and more. "
                "Use SELECT ... FROM <Entity> syntax."
            ),
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                description_builder=lambda args: (
                    f"Query QuickBooks: {str(args.get('query', ''))[:60]}"
                ),
            ),
        ),
        Tool(
            name=ToolName.QB_CREATE,
            description=(
                "Create an entity in QuickBooks Online. Pass the entity type "
                "(Customer, Estimate, or Invoice) and the QBO API payload. "
                "See the QuickBooks skill for payload formats and examples."
            ),
            function=qb_create,
            params_model=QBCreateParams,
            usage_hint=(
                "Create a Customer, Estimate, or Invoice in QB. "
                "Construct the QBO API payload as described in the skill docs."
            ),
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                description_builder=lambda args: (
                    f"Create {args.get('entity_type', 'entity')} in QuickBooks"
                ),
            ),
        ),
        Tool(
            name=ToolName.QB_UPDATE,
            description=(
                "Update an existing entity in QuickBooks Online. Pass the entity type "
                "(Customer, Estimate, or Invoice) and the full QBO API payload "
                "including Id and SyncToken from a prior qb_query. "
                "See the QuickBooks skill for payload formats."
            ),
            function=qb_update,
            params_model=QBUpdateParams,
            usage_hint=(
                "Update a Customer, Estimate, or Invoice in QB. "
                "Payload must include Id and SyncToken from a prior query."
            ),
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                description_builder=lambda args: (
                    f"Update {args.get('entity_type', 'entity')} in QuickBooks"
                ),
            ),
        ),
        Tool(
            name=ToolName.QB_SEND,
            description=(
                "Send an invoice or estimate to a customer via QuickBooks email. "
                "The entity must already exist in QuickBooks."
            ),
            function=qb_send,
            params_model=QBSendParams,
            usage_hint=(
                "Send a QB invoice or estimate by email. "
                "Confirm the email address with the user first."
            ),
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                description_builder=lambda args: (
                    f"Send {args.get('entity_type', 'entity')} "
                    f"to {args.get('email', 'recipient')} via QuickBooks"
                ),
            ),
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
    from backend.app.agent.tools.registry import SubToolInfo, default_registry

    default_registry.register(
        "quickbooks",
        _quickbooks_factory,
        core=False,
        summary=(
            "Query, create, and manage QuickBooks Online entities: "
            "invoices, estimates, customers, and more"
        ),
        sub_tools=[
            SubToolInfo(ToolName.QB_QUERY, "Run read-only queries against QuickBooks Online"),
            SubToolInfo(ToolName.QB_CREATE, "Create entities in QuickBooks"),
            SubToolInfo(ToolName.QB_UPDATE, "Update existing entities in QuickBooks"),
            SubToolInfo(ToolName.QB_SEND, "Send invoices or estimates via QuickBooks email"),
        ],
    )


_register()
