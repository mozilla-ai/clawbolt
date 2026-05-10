"""Work-order read tools for AppFolio Vendor Portal.

PR1 surface: list, search, get. Write tools (accept, schedule,
status updates, notes with photos) ship in PR2.
"""

from __future__ import annotations

import logging
from typing import Any

from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.integrations.appfolio_vendor.errors import log_unexpected_response_shape
from backend.app.integrations.appfolio_vendor.params import (
    AppFolioGetWorkOrderParams,
    AppFolioListWorkOrdersParams,
    AppFolioSearchWorkOrdersParams,
)
from backend.app.integrations.appfolio_vendor.service import (
    AppFolioError,
    AppFolioVendorService,
    AuthExpiredError,
)

logger = logging.getLogger(__name__)


_AUTH_EXPIRED_HINT = (
    "AppFolio session expired. Tell the user to request a fresh magic link"
    " from vendor.appfolio.com and re-run appfolio_connect."
)


def _service_error(method_label: str, exc: Exception) -> ToolResult:
    if isinstance(exc, AuthExpiredError):
        return ToolResult(
            content=f"AppFolio session expired while {method_label}.",
            is_error=True,
            error_kind=ToolErrorKind.AUTH,
            hint=_AUTH_EXPIRED_HINT,
        )
    if isinstance(exc, AppFolioError):
        return ToolResult(
            content=f"AppFolio error while {method_label}: {exc}",
            is_error=True,
            error_kind=ToolErrorKind.SERVICE,
        )
    logger.exception("Unexpected AppFolio failure %s", method_label)
    return ToolResult(
        content=f"Unexpected error while {method_label}: {exc}",
        is_error=True,
        error_kind=ToolErrorKind.INTERNAL,
    )


def _fmt_work_order_line(wo: dict[str, Any]) -> str:
    wo_id = wo.get("id") or wo.get("work_order_id") or "?"
    # ``numberForDisplay`` is the WO# rendered in the Vendor Portal UI; it
    # can differ from the API ``id``. Prefer it so the agent's "WO #X"
    # matches what the user sees in their AppFolio dashboard. Fall back to
    # other plausible field names for forward compat, then to ``id``.
    number = (
        wo.get("numberForDisplay")
        or wo.get("number_for_display")
        or wo.get("work_order_number")
        or wo.get("number")
        or wo_id
    )
    customer_id = wo.get("customer_id") or wo.get("customerId") or ""
    status = wo.get("status") or wo.get("status_label") or wo.get("statusCode") or ""
    address = wo.get("property_address") or wo.get("address") or wo.get("propertyAddress") or ""
    summary = wo.get("description") or wo.get("title") or wo.get("summary") or ""
    pieces = [f"#{number}"]
    if customer_id:
        # Surface the customer_id so the agent can pass it back to write
        # endpoints (notes, invoices) without an extra round-trip. The
        # vendor's "customer" is their property-management company.
        pieces.append(f"customer_id={customer_id}")
    if status:
        pieces.append(f"[{status}]")
    if address:
        pieces.append(str(address))
    if summary:
        pieces.append(str(summary)[:100])
    return f"- ID: {wo_id} | " + " | ".join(pieces)


_KNOWN_WO_LIST_ENVELOPES = ("work_orders", "workOrders", "results", "data")


def _normalize_list(payload: Any) -> list[dict[str, Any]]:
    """Return a list of work-order dicts from whichever envelope AppFolio used.

    Returns ``[]`` when the response shape is not one we recognize; the
    *caller* is responsible for logging that case via
    :func:`log_unexpected_response_shape` so the empty-list semantics
    stay simple and the diagnostic carries the calling tool's label.
    """
    if isinstance(payload, list):
        return [w for w in payload if isinstance(w, dict)]
    if isinstance(payload, dict):
        for key in _KNOWN_WO_LIST_ENVELOPES:
            value = payload.get(key)
            if isinstance(value, list):
                return [w for w in value if isinstance(w, dict)]
    return []


def build_work_order_tools(service: AppFolioVendorService) -> list[Tool]:
    """Return the AppFolio work-order read tools."""

    async def appfolio_list_work_orders(
        include_in_progress: bool = True,
        include_completed: bool = False,
        include_estimates: bool = True,
        customer_id: str = "",
    ) -> ToolResult:
        try:
            payload = await service.list_work_orders(
                include_in_progress=include_in_progress,
                include_completed=include_completed,
                include_estimates=include_estimates,
                customer_id=customer_id or None,
            )
        except Exception as exc:
            return _service_error("listing work orders", exc)

        items = _normalize_list(payload)
        if not items:
            # An empty list is a legitimate result, but if the response
            # was a non-empty dict the shape is probably the issue (we
            # didn't recognize the envelope). Log so the next mismatch
            # is debuggable rather than just looking like "no results".
            if isinstance(payload, dict) and payload:
                log_unexpected_response_shape(
                    "appfolio_list_work_orders",
                    payload,
                    expected=(
                        "list of work-order dicts, or a dict with one of "
                        f"{list(_KNOWN_WO_LIST_ENVELOPES)} containing the list"
                    ),
                )
            return ToolResult(content="No matching work orders.")
        lines = [f"Found {len(items)} work order(s):"]
        lines.extend(_fmt_work_order_line(w) for w in items[:30])
        if len(items) > 30:
            lines.append(f"... and {len(items) - 30} more (use search to narrow).")
        return ToolResult(content="\n".join(lines))

    async def appfolio_search_work_orders(search_term: str) -> ToolResult:
        try:
            payload = await service.search_work_orders(search_term)
        except Exception as exc:
            return _service_error("searching work orders", exc)

        items = _normalize_list(payload)
        if not items:
            if isinstance(payload, dict) and payload:
                log_unexpected_response_shape(
                    f"appfolio_search_work_orders(search_term={search_term!r})",
                    payload,
                    expected=(
                        "list of work-order dicts, or a dict with one of "
                        f"{list(_KNOWN_WO_LIST_ENVELOPES)} containing the list"
                    ),
                )
            return ToolResult(content=f"No work orders matched '{search_term}'.")
        lines = [f"{len(items)} match(es) for '{search_term}':"]
        lines.extend(_fmt_work_order_line(w) for w in items[:20])
        return ToolResult(content="\n".join(lines))

    async def appfolio_get_work_order(customer_id: str, work_order_id: str) -> ToolResult:
        try:
            wo = await service.get_work_order(customer_id, work_order_id)
        except Exception as exc:
            return _service_error("fetching work order", exc)

        if not isinstance(wo, dict) or not wo:
            return ToolResult(
                content=f"Work order {work_order_id} not found.",
                is_error=True,
                error_kind=ToolErrorKind.NOT_FOUND,
            )

        # Best-effort second call for richer details (notes count, attachments).
        details: dict[str, Any] = {}
        try:
            raw = await service.get_work_order_details(work_order_id)
            if isinstance(raw, dict):
                details = raw
        except AuthExpiredError as exc:
            return _service_error("fetching work order details", exc)
        except AppFolioError as exc:
            logger.info("work_order_details unavailable for %s: %s", work_order_id, exc)

        # If none of the expected fields are present, the response shape
        # has likely drifted; surface a diagnostic so we don't silently
        # render a "Work order #X | Status: ? | Address: ?" stub.
        recognised_fields = (
            "work_order_number",
            "status",
            "status_label",
            "property_address",
            "address",
            "description",
            "summary",
        )
        if not any(wo.get(k) for k in recognised_fields):
            log_unexpected_response_shape(
                f"appfolio_get_work_order(customer_id={customer_id}, "
                f"work_order_id={work_order_id})",
                wo,
                expected=(
                    f"work-order dict with at least one of {list(recognised_fields)} populated"
                ),
            )
        lines = [
            f"Work order #{wo.get('work_order_number') or work_order_id}",
            f"  Status: {wo.get('status') or wo.get('status_label') or '?'}",
            f"  Address: {wo.get('property_address') or wo.get('address') or '?'}",
        ]
        if wo.get("description") or wo.get("summary"):
            lines.append(f"  Summary: {wo.get('description') or wo.get('summary')}")
        if details.get("workOrderInvoiceAttachments"):
            count = len(details["workOrderInvoiceAttachments"])
            lines.append(f"  Attachments: {count}")
        return ToolResult(content="\n".join(lines))

    return [
        Tool(
            name=ToolName.APPFOLIO_LIST_WORK_ORDERS,
            description=(
                "List the user's AppFolio work orders, filtered by status."
                " Default returns in-progress and estimates needed."
            ),
            function=appfolio_list_work_orders,
            params_model=AppFolioListWorkOrdersParams,
            usage_hint=(
                "Use to give the user a status summary of their open work."
                " For specific lookups (by address or work order number), use"
                " appfolio_search_work_orders instead."
            ),
        ),
        Tool(
            name=ToolName.APPFOLIO_SEARCH_WORK_ORDERS,
            description="Search AppFolio work orders by number, address, or free text.",
            function=appfolio_search_work_orders,
            params_model=AppFolioSearchWorkOrdersParams,
            usage_hint=(
                "Pass any free text the user gave you: a property address,"
                " a tenant name, or a work order number."
            ),
        ),
        Tool(
            name=ToolName.APPFOLIO_GET_WORK_ORDER,
            description="Get full details for a single AppFolio work order.",
            function=appfolio_get_work_order,
            params_model=AppFolioGetWorkOrderParams,
            usage_hint=(
                "Use after a list or search to drill into one work order."
                " Both customer_id and work_order_id come from the list output."
            ),
        ),
    ]
