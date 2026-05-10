"""Payment read tools for AppFolio Vendor Portal."""

from __future__ import annotations

import logging
from typing import Any

from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.integrations.appfolio_vendor.errors import log_unexpected_response_shape
from backend.app.integrations.appfolio_vendor.params import AppFolioListPaymentsParams
from backend.app.integrations.appfolio_vendor.service import (
    AppFolioError,
    AppFolioVendorService,
    AuthExpiredError,
)

logger = logging.getLogger(__name__)


_KNOWN_PAYMENT_LIST_ENVELOPES = ("payments", "data", "results", "vendor_portal_payable_payments")


def _normalize_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    if isinstance(payload, dict):
        for key in _KNOWN_PAYMENT_LIST_ENVELOPES:
            value = payload.get(key)
            if isinstance(value, list):
                return [p for p in value if isinstance(p, dict)]
    return []


def _fmt_payment_line(p: dict[str, Any]) -> str:
    pid = p.get("id") or "?"
    amount = p.get("amount") or p.get("total") or ""
    posted = p.get("posted_on") or p.get("date") or p.get("postedOn") or ""
    method = p.get("settlement_method") or p.get("method") or ""
    status = p.get("status") or ""
    parts = [f"- ID: {pid}"]
    if amount:
        parts.append(f"${amount}")
    if posted:
        parts.append(str(posted))
    if method:
        parts.append(str(method))
    if status:
        parts.append(f"[{status}]")
    return " | ".join(parts)


def build_payment_tools(service: AppFolioVendorService) -> list[Tool]:
    """Return AppFolio payment read tools."""

    async def appfolio_list_payments(
        posted_on: str = "",
        settlement_method: str = "",
    ) -> ToolResult:
        try:
            payload = await service.list_payments(
                posted_on=posted_on or None,
                settlement_method=settlement_method or None,
            )
        except AuthExpiredError:
            return ToolResult(
                content="AppFolio session expired while listing payments.",
                is_error=True,
                error_kind=ToolErrorKind.AUTH,
                hint=("Tell the user to request a fresh magic link and re-run appfolio_connect."),
            )
        except AppFolioError as exc:
            return ToolResult(
                content=f"AppFolio payments lookup failed: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )
        except Exception as exc:
            logger.exception("AppFolio payments fetch unexpected failure")
            return ToolResult(
                content=f"Unexpected payments error: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.INTERNAL,
            )

        items = _normalize_list(payload)
        if not items:
            if isinstance(payload, dict) and payload:
                log_unexpected_response_shape(
                    "appfolio_list_payments",
                    payload,
                    expected=(
                        "list of payment dicts, or a dict with one of "
                        f"{list(_KNOWN_PAYMENT_LIST_ENVELOPES)} containing the list"
                    ),
                )
            return ToolResult(content="No payments found for those filters.")
        lines = [f"Found {len(items)} payment(s):"]
        lines.extend(_fmt_payment_line(p) for p in items[:30])
        if len(items) > 30:
            lines.append(f"... and {len(items) - 30} more.")
        return ToolResult(content="\n".join(lines))

    return [
        Tool(
            name=ToolName.APPFOLIO_LIST_PAYMENTS,
            description="List AppFolio payments, optionally filtered by date or method.",
            function=appfolio_list_payments,
            params_model=AppFolioListPaymentsParams,
            usage_hint=(
                "Use when the user asks 'did I get paid', 'show recent payments',"
                " or wants to verify a specific transaction."
            ),
        ),
    ]
