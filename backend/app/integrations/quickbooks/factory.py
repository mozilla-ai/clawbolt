"""QuickBooks Online tools for the agent."""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import BaseModel, Field, field_validator

from backend.app.agent.approval import ApprovalPolicy, PermissionLevel
from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolReceipt, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.config import settings
from backend.app.integrations.quickbooks.service import (
    QuickBooksOnlineService,
    QuickBooksService,
)
from backend.app.services.oauth import (
    _get_intuit_endpoints,
    oauth_service,
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

# Human-readable labels for queryable entities.
_ENTITY_LABELS: dict[str, str] = {
    "INVOICE": "invoices",
    "ESTIMATE": "estimates",
    "CUSTOMER": "customers",
    "ITEM": "items",
    "PAYMENT": "payments",
    "BILL": "bills",
    "VENDOR": "vendors",
    "SALESRECEIPT": "sales receipts",
    "CREDITMEMO": "credit memos",
    "PURCHASEORDER": "purchase orders",
    "TIMEACTIVITY": "time entries",
    "DEPOSIT": "deposits",
    "TRANSFER": "transfers",
    "JOURNALENTRY": "journal entries",
}

# Entity types that qb_create is allowed to create.
_CREATABLE_ENTITIES = {"Customer", "Estimate", "Invoice", "Item"}

# Entity types that qb_update is allowed to update.
_UPDATABLE_ENTITIES = {"Customer", "Estimate", "Invoice", "Item"}

# Entity types that qb_send is allowed to send via email.
_SENDABLE_ENTITIES = {"Invoice", "Estimate"}


class QBQueryParams(BaseModel):
    """Parameters for the qb_query tool."""

    query: str = Field(
        description=(
            "A QBO query string (SELECT only). Example: SELECT * FROM Invoice MAXRESULTS 20.\n"
            "Common string-enum values that the model often gets wrong:\n"
            "  Estimate.TxnStatus = 'Pending' | 'Accepted' | 'Closed' | 'Rejected'\n"
            "  Invoice.EmailStatus = 'NotSet' | 'NeedToSend' | 'EmailSent'\n"
            "  Bill / Invoice / Estimate balance filtering uses Balance numeric, "
            "  not a status enum."
        )
    )


_TXNSTATUS_VALID_BY_ENTITY: dict[str, tuple[str, ...]] = {
    "Estimate": ("Pending", "Accepted", "Closed", "Rejected"),
}
"""Known string-enum value sets per entity. Used to enrich 400 errors so a
hallucinated value like ``TxnStatus='In Progress'`` comes back with the
right list instead of the model retrying the same wrong guess."""


def _format_intuit_fault(exc: httpx.HTTPStatusError, *, entity: str | None = None) -> str:
    """Convert a QBO HTTPStatusError into a model-readable error message.

    Intuit returns a structured ``Fault.Error[]`` payload that the LLM
    has trouble parsing reliably. We pull out ``Message`` + ``Detail``
    + ``code`` and concatenate them. When ``Detail`` mentions a value
    that maps to a known enum (``TxnStatus`` on Estimate so far), we
    append the valid set so the next turn does not retry the same bad
    guess.

    Falls back to the raw JSON (then the raw exception string) when the
    response is not JSON or is shaped differently than expected.
    """
    raw_body: Any
    try:
        raw_body = exc.response.json()
    except Exception:
        return f"HTTP {exc.response.status_code} from QuickBooks: {exc.response.text or exc!s}"

    fault = (raw_body or {}).get("Fault") if isinstance(raw_body, dict) else None
    errors = (fault or {}).get("Error") if isinstance(fault, dict) else None
    if not isinstance(errors, list) or not errors:
        return f"HTTP {exc.response.status_code} from QuickBooks: {json.dumps(raw_body)[:500]}"

    parts: list[str] = []
    for err in errors:
        if not isinstance(err, dict):
            continue
        code = err.get("code", "")
        message = (err.get("Message") or "").strip()
        detail = (err.get("Detail") or "").strip()
        # Most-specific first: Detail usually contains the bad value;
        # Message is the category.
        line = " | ".join(p for p in (f"code={code}" if code else "", message, detail) if p)
        if line:
            parts.append(line)

    body = "; ".join(parts) or json.dumps(raw_body)[:500]

    # Enrich when the failure looks like a hallucinated enum value. The
    # set is small and curated so the false-positive rate stays low.
    hint = _enum_hint(body, entity)
    if hint:
        body = f"{body}\nHint: {hint}"
    return body


def _enum_hint(error_body: str, entity: str | None) -> str:
    """Map a QBO error blob to a one-line hint when it looks enum-shaped.

    Intentionally conservative: matches on substrings the LLM is likely
    to also see. Returns empty string when no hint applies.
    """
    if not entity:
        return ""
    lowered = error_body.lower()
    if "txnstatus" in lowered and entity in _TXNSTATUS_VALID_BY_ENTITY:
        valid = ", ".join(_TXNSTATUS_VALID_BY_ENTITY[entity])
        return f"Valid TxnStatus for {entity}: {valid}"
    return ""


def _coerce_data_to_dict(value: Any) -> Any:
    """Parse JSON-encoded strings into dicts so the LLM can pass either shape.

    The LLM occasionally over-quotes deeply nested QBO payloads and emits
    `data` as a JSON string rather than a JSON object. Accept both forms
    on the first round to avoid a wasted retry.
    """
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "data must be a JSON object or a JSON-encoded object string; "
                f"could not parse string as JSON: {exc.msg}"
            ) from exc
        if not isinstance(parsed, dict):
            raise ValueError(
                f"data must be a JSON object; got a JSON-encoded {type(parsed).__name__}"
            )
        return parsed
    return value


class QBCreateParams(BaseModel):
    """Parameters for the qb_create tool."""

    entity_type: str = Field(
        description="QBO entity type to create: Customer, Estimate, Invoice, or Item"
    )
    data: dict[str, Any] = Field(
        description=(
            "QBO API payload for the entity as a JSON object. See SKILL.md for payload formats."
        )
    )

    _coerce_data = field_validator("data", mode="before")(_coerce_data_to_dict)


class QBUpdateParams(BaseModel):
    """Parameters for the qb_update tool."""

    entity_type: str = Field(
        description="QBO entity type to update: Customer, Estimate, Invoice, or Item"
    )
    data: dict[str, Any] = Field(
        description=(
            "Full QBO API payload as a JSON object, "
            "including Id and SyncToken from a prior qb_query. "
            "See SKILL.md for payload formats."
        )
    )

    _coerce_data = field_validator("data", mode="before")(_coerce_data_to_dict)


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
                if "name" in val or "value" in val:
                    name = val.get("name", "")
                    ref_val = val.get("value", "")
                    if name and ref_val:
                        parts.append(f"{key}: {name} ({ref_val})")
                    elif name or ref_val:
                        parts.append(f"{key}: {name or ref_val}")
                elif "Address" in val:
                    parts.append(f"{key}: {val['Address']}")
                elif "FreeFormNumber" in val:
                    parts.append(f"{key}: {val['FreeFormNumber']}")
                elif "URI" in val:
                    parts.append(f"{key}: {val['URI']}")
                elif any(k in val for k in ("Line1", "City", "PostalCode")):
                    addr_bits = [
                        val[k]
                        for k in (
                            "Line1",
                            "Line2",
                            "City",
                            "CountrySubDivisionCode",
                            "PostalCode",
                        )
                        if val.get(k)
                    ]
                    if addr_bits:
                        parts.append(f"{key}: {', '.join(addr_bits)}")
                else:
                    # Fail loud on unknown dict shapes so future QBO fields
                    # surface (verbose but visible) rather than disappearing.
                    parts.append(f"{key}: {json.dumps(val)}")
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


def _extract_query_entity(args: dict[str, Any]) -> str | None:
    """Extract the entity name from a QBO query string (e.g. 'Invoice' from 'SELECT * FROM Invoice')."""
    query = str(args.get("query", ""))
    match = re.search(r"\bFROM\s+(\w+)", query, re.IGNORECASE)
    return match.group(1) if match else None


def _describe_qb_query(args: dict[str, Any]) -> str:
    """Build a human-readable description for a QuickBooks query."""
    query = str(args.get("query", ""))
    match = re.search(r"\bFROM\s+(\w+)", query, re.IGNORECASE)
    if not match:
        return "Look up data in QuickBooks"
    entity = match.group(1).upper()
    label = _ENTITY_LABELS.get(entity, match.group(1).lower() + "s")
    return f"Look up {label} in QuickBooks"


def _extract_entity_type(args: dict[str, Any]) -> str | None:
    """Extract the entity_type argument."""
    return str(args["entity_type"]) if args.get("entity_type") else None


# Entity types whose payload carries a ``Line`` array we render in the
# approval prompt. Customer payloads do not have line items, so they
# stay on the short legacy form.
_LINE_ITEMIZED_ENTITIES: frozenset[str] = frozenset({"Invoice", "Estimate"})


def _qb_approval_header(verb: str, entity_type: str, entity_id: Any, total: float | None) -> str:
    """Build the first line of a qb_create / qb_update approval prompt.

    Update headers include ``#{Id}`` (when available) so audit-log review
    can trace which row was edited; Create headers do not because the
    id only exists after QBO assigns one on the POST response. When
    ``total`` is set, the header appends ``for ${total:,.2f}``.

    Thousands separator matches ``_receipt_target`` so the same invoice
    formats consistently across the approval prompt and the post-write
    ToolReceipt.
    """
    pieces = [f"{verb} {entity_type}"]
    if verb == "Update" and entity_id:
        pieces[0] = f"{pieces[0]} #{entity_id}"
    pieces.append("in QuickBooks")
    if total is not None:
        pieces.append(f"for ${total:,.2f}")
    return " ".join(pieces)


def _format_qb_write_approval_description(verb: str, args: dict[str, Any]) -> str:
    """Render a multi-line approval prompt that names every line item.

    The default builder used to print only ``"Create Invoice in
    QuickBooks"``, which let a billing action slip through with the
    wrong per-line math because the user could not see quantity, unit
    price, or total before approving. Surfacing each line as
    ``qty x $unit = $line_total`` (and the grand total) makes the
    approval prompt the last place a mistake can be caught before
    QuickBooks stores it. Mirrors the AppFolio fix in #1292.

    Falls back to the short form (``"<verb> <entity_type> in
    QuickBooks"``) for entity types without line items (Customer) and
    on any malformed payload, so the prompt never crashes; the agent's
    own typed validation will reject a bad call after approval anyway.

    ``verb`` is "Create" or "Update". For Update the header includes
    the entity Id so an admin reviewing audit logs can trace which row
    was edited.

    Trusts that ``args`` came from ``QBCreateParams`` / ``QBUpdateParams``
    (Pydantic-validated upstream), so ``args["data"]`` is a dict;
    line-shape tolerance below is for the LLM's freeform payload
    *inside* ``data``, not for the param envelope itself.
    """
    entity_type = str(args.get("entity_type") or "entity")
    data: dict[str, Any] = args.get("data") or {}
    entity_id = data.get("Id")

    short_header = _qb_approval_header(verb, entity_type, entity_id, total=None)

    if entity_type not in _LINE_ITEMIZED_ENTITIES:
        return short_header

    lines_raw = data.get("Line")
    if not isinstance(lines_raw, list) or not lines_raw:
        return short_header

    parsed: list[tuple[str, float | None, float | None, float]] = []
    grand_total = 0.0
    for line in lines_raw:
        if not isinstance(line, dict):
            return short_header
        try:
            amount = float(line.get("Amount", 0) or 0)
        except (TypeError, ValueError):
            return short_header
        description = str(line.get("Description") or "(no description)")
        detail = line.get("SalesItemLineDetail")
        qty: float | None = None
        unit_price: float | None = None
        if isinstance(detail, dict):
            try:
                if detail.get("Qty") is not None:
                    qty = float(detail["Qty"])
                if detail.get("UnitPrice") is not None:
                    unit_price = float(detail["UnitPrice"])
            except (TypeError, ValueError):
                qty = None
                unit_price = None
        grand_total += amount
        parsed.append((description, qty, unit_price, amount))

    if not parsed:
        return short_header

    rendered = [_qb_approval_header(verb, entity_type, entity_id, total=grand_total)]
    for idx, (description, qty, unit_price, amount) in enumerate(parsed, start=1):
        short_desc = description if len(description) <= 80 else description[:77] + "..."
        if qty is not None and unit_price is not None:
            # ``:g`` drops trailing ``.0`` so ``5.0`` reads as ``5`` while
            # leaving genuine fractional quantities (e.g. ``1.5``) intact.
            rendered.append(
                f"  {idx}. {short_desc} | qty {qty:g} x ${unit_price:,.2f} = ${amount:,.2f}"
            )
        else:
            rendered.append(f"  {idx}. {short_desc} | ${amount:,.2f}")
    return "\n".join(rendered)


# Entity types that have a public QBO web UI page we can deep-link to.
_WEB_LINKABLE_ENTITIES: dict[str, str] = {
    "Invoice": "invoice",
    "Estimate": "estimate",
    "Customer": "customerdetail",
}


def _build_qbo_url(qb_service: QuickBooksService, entity_type: str, entity_id: str) -> str | None:
    """Build a deep link into the QuickBooks Online web UI for an entity.

    Returns ``None`` for entity types without a known web UI path. The link
    always points at the real entity ID returned by the API, so no LLM text
    is involved.
    """
    path = _WEB_LINKABLE_ENTITIES.get(entity_type)
    if not path or not entity_id or entity_id == "?":
        return None
    qbo_service = qb_service if isinstance(qb_service, QuickBooksOnlineService) else None
    if qbo_service is None:
        return None
    is_prod = "sandbox" not in qbo_service._api_base
    host = "app.qbo.intuit.com" if is_prod else "app.sandbox.qbo.intuit.com"
    if entity_type == "Customer":
        return f"https://{host}/app/{path}?nameId={entity_id}"
    return f"https://{host}/app/{path}?txnId={entity_id}"


def _receipt_target(entity_type: str, result: dict[str, Any]) -> str:
    """Best-effort human-readable target string for a QB entity result."""
    total = result.get("TotalAmt")
    name = result.get("DisplayName") or ""
    doc_num = result.get("DocNumber") or ""
    entity_id = result.get("Id", "")
    if entity_type in ("Invoice", "Estimate"):
        customer_ref = result.get("CustomerRef") or {}
        customer = customer_ref.get("name", "") if isinstance(customer_ref, dict) else ""
        bits: list[str] = []
        if customer:
            bits.append(customer)
        if total is not None:
            bits.append(f"${total:,.2f}")
        if not bits and doc_num:
            bits.append(f"#{doc_num}")
        if not bits:
            bits.append(f"ID {entity_id}")
        return ", ".join(bits)
    if entity_type == "Customer":
        return name or f"ID {entity_id}"
    if entity_type == "Item":
        item_name = result.get("Name") or ""
        return item_name or f"ID {entity_id}"
    return name or doc_num or f"ID {entity_id}"


def _extract_send_email(args: dict[str, Any]) -> str | None:
    """Extract the email recipient from qb_send arguments."""
    return str(args["email"]) if args.get("email") else None


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

        # Resolve the entity name from the SELECT clause once so the
        # error formatter can attach the right enum-hint set when QBO
        # returns a 400.
        entity_name = entity_match.group(1).capitalize()

        try:
            rows = await qb_service.query(normalized)
        except Exception as exc:
            logger.exception("QuickBooks query failed")
            if isinstance(exc, httpx.HTTPStatusError):
                error_str = _format_intuit_fault(exc, entity=entity_name)
            else:
                error_str = str(exc)
            return ToolResult(
                content=f"QuickBooks query error: {error_str}",
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
            if isinstance(exc, httpx.HTTPStatusError):
                error_str = _format_intuit_fault(exc, entity=entity_type)
            else:
                error_str = str(exc)
            return ToolResult(
                content=f"Failed to create {entity_type}: {error_str}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

        entity_id = result.get("Id", "?")
        doc_num = result.get("DocNumber", "")
        total = result.get("TotalAmt")
        display_name = result.get("DisplayName", "")
        item_name = result.get("Name", "")

        # LLM-facing content stays terse and data-only so the model has no
        # receipt-shaped phrasing to bullet-point back to the user. The
        # auto-appended ToolReceipt is the canonical user-facing rendering
        # (see regression test guarding ``qb_send`` content).
        parts = ["ok", f"Id: {entity_id}"]
        if doc_num:
            parts.append(f"DocNumber: {doc_num}")
        if total is not None:
            parts.append(f"Total: ${total:.2f}")
        if display_name:
            parts.append(f"Name: {display_name}")
        if not display_name and item_name:
            parts.append(f"Name: {item_name}")

        return ToolResult(
            content=" | ".join(parts),
            receipt=ToolReceipt(
                action=f"Created QuickBooks {entity_type.lower()} for",
                target=_receipt_target(entity_type, result),
                url=_build_qbo_url(qb_service, entity_type, str(entity_id)),
            ),
        )

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
            if isinstance(exc, httpx.HTTPStatusError):
                error_str = _format_intuit_fault(exc, entity=entity_type)
            else:
                error_str = str(exc)
            return ToolResult(
                content=f"Failed to update {entity_type}: {error_str}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

        entity_id = result.get("Id", "?")
        doc_num = result.get("DocNumber", "")
        total = result.get("TotalAmt")
        display_name = result.get("DisplayName", "")
        item_name = result.get("Name", "")

        parts = ["ok", f"Id: {entity_id}"]
        if doc_num:
            parts.append(f"DocNumber: {doc_num}")
        if total is not None:
            parts.append(f"Total: ${total:.2f}")
        if display_name:
            parts.append(f"Name: {display_name}")
        if not display_name and item_name:
            parts.append(f"Name: {item_name}")

        return ToolResult(
            content=" | ".join(parts),
            receipt=ToolReceipt(
                action=f"Updated QuickBooks {entity_type.lower()} for",
                target=_receipt_target(entity_type, result),
                url=_build_qbo_url(qb_service, entity_type, str(entity_id)),
            ),
        )

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
            if isinstance(exc, httpx.HTTPStatusError):
                error_str = _format_intuit_fault(exc, entity=entity_type)
            else:
                error_str = str(exc)
            return ToolResult(
                content=f"Failed to send {entity_type.lower()}: {error_str}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

        # Drop the verb + recipient from the LLM-facing content: that
        # phrasing was getting bullet-pointed in prose right before the
        # auto-receipt rendered the same action structurally, producing
        # a double-bullet for one underlying call. Bug observed in
        # production 2026-05-13 on a contractor's invoice send.
        return ToolResult(
            content=f"ok | {entity_type} Id: {entity_id}",
            receipt=ToolReceipt(
                action=f"Emailed QuickBooks {entity_type.lower()} to",
                target=email,
                url=_build_qbo_url(qb_service, entity_type, entity_id),
            ),
        )

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
                resource_extractor=_extract_query_entity,
                description_builder=_describe_qb_query,
            ),
        ),
        Tool(
            name=ToolName.QB_CREATE,
            description=(
                "Create an entity in QuickBooks Online. Pass the entity type "
                "(Customer, Estimate, Invoice, or Item) and the QBO API payload. "
                "See the QuickBooks skill for payload formats and examples."
            ),
            function=qb_create,
            params_model=QBCreateParams,
            usage_hint=(
                "Create a Customer, Estimate, Invoice, or Item in QB. "
                "Construct the QBO API payload as described in the skill docs."
            ),
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                resource_extractor=_extract_entity_type,
                description_builder=lambda args: _format_qb_write_approval_description(
                    "Create", args
                ),
            ),
        ),
        Tool(
            name=ToolName.QB_UPDATE,
            description=(
                "Update an existing entity in QuickBooks Online. Pass the entity type "
                "(Customer, Estimate, Invoice, or Item) and the full QBO API payload "
                "including Id and SyncToken from a prior qb_query. "
                "See the QuickBooks skill for payload formats."
            ),
            function=qb_update,
            params_model=QBUpdateParams,
            usage_hint=(
                "Update a Customer, Estimate, Invoice, or Item in QB. "
                "Payload must include Id and SyncToken from a prior query."
            ),
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                resource_extractor=_extract_entity_type,
                description_builder=lambda args: _format_qb_write_approval_description(
                    "Update", args
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
                resource_extractor=_extract_send_email,
                description_builder=lambda args: (
                    f"Send {args.get('entity_type', 'entity')} "
                    f"to {args.get('email', 'recipient')} via QuickBooks"
                ),
            ),
        ),
    ]


async def _get_quickbooks_service_for_user(user_id: str) -> QuickBooksService | None:
    """Build a QuickBooks service using OAuth tokens for the given user."""
    token = await oauth_service.get_valid_token(user_id, "quickbooks")
    if token and token.access_token and token.realm_id:
        _, token_url = _get_intuit_endpoints()
        return QuickBooksOnlineService(
            client_id=settings.quickbooks_client_id,
            client_secret=settings.quickbooks_client_secret,
            realm_id=token.realm_id,
            access_token=token.access_token,
            refresh_token=token.refresh_token,
            environment=settings.quickbooks_environment,
            on_token_refresh=oauth_service.build_on_refresh_callback(user_id, "quickbooks"),
            token_url=token_url,
        )
    return None


async def _quickbooks_auth_check(ctx: ToolContext) -> str | None:
    """Check whether QuickBooks is configured and the user has authenticated.

    Returns ``None`` when ready, or a reason string when auth is missing.
    Returns ``None`` (not a reason) when the integration is not configured
    at all (admin has not set credentials), so it stays completely hidden.
    """
    if not settings.quickbooks_client_id or not settings.quickbooks_client_secret:
        return None
    token = await oauth_service.load_token(ctx.user.id, "quickbooks")
    if token and token.access_token and token.realm_id:
        return None
    return (
        "QuickBooks is not connected. "
        "Use manage_integration(action='connect', target='quickbooks') "
        "to generate a connection link for the user."
    )


async def _quickbooks_factory(ctx: ToolContext) -> list[Tool]:
    """Factory for QuickBooks tools, used by the registry."""
    if not settings.quickbooks_client_id or not settings.quickbooks_client_secret:
        return []
    qb_service = await _get_quickbooks_service_for_user(ctx.user.id)
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
        display_name="QuickBooks Online",
        dashboard_description="Query, create, and manage QuickBooks Online entities",
        dashboard_group="Integrations",
        dashboard_group_order=2,
        sub_tools=[
            SubToolInfo(
                ToolName.QB_QUERY,
                "Run read-only queries against QuickBooks Online",
                default_permission="ask",
            ),
            SubToolInfo(
                ToolName.QB_CREATE, "Create entities in QuickBooks", default_permission="ask"
            ),
            SubToolInfo(
                ToolName.QB_UPDATE,
                "Update existing entities in QuickBooks",
                default_permission="ask",
            ),
            SubToolInfo(
                ToolName.QB_SEND,
                "Send invoices or estimates via QuickBooks email",
                default_permission="ask",
            ),
        ],
        auth_check=_quickbooks_auth_check,
    )


_register()
