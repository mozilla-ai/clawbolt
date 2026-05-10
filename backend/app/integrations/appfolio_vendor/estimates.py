"""Estimate tools for AppFolio Vendor Portal."""

from __future__ import annotations

import logging
from typing import Any

from backend.app.agent.approval import ApprovalPolicy, PermissionLevel
from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolReceipt, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.integrations.appfolio_vendor.errors import (
    log_unexpected_response_shape,
    service_error_to_tool_result,
)
from backend.app.integrations.appfolio_vendor.params import (
    AppFolioGetEstimateParams,
    AppFolioUpdateEstimateParams,
)
from backend.app.integrations.appfolio_vendor.service import AppFolioVendorService

logger = logging.getLogger(__name__)


def _fmt_estimate(payload: Any) -> str:
    """Render the JSON:API estimate envelope into a compact summary."""
    if not isinstance(payload, dict):
        return "AppFolio returned an unexpected estimate shape."
    data = payload.get("data") if "data" in payload else payload
    if not isinstance(data, dict):
        return "AppFolio returned no estimate data."
    attrs = data.get("attributes") if isinstance(data.get("attributes"), dict) else data
    estimate_id = data.get("id") or data.get("estimate_id") or "?"
    amount = attrs.get("amount") or attrs.get("total") or ""
    description = attrs.get("description") or ""
    status = attrs.get("status") or ""
    lines = [f"Estimate {estimate_id}"]
    if status:
        lines.append(f"  Status: {status}")
    if amount:
        lines.append(f"  Amount: {amount}")
    if description:
        lines.append(f"  Description: {description}")
    attachments = payload.get("included") if isinstance(payload, dict) else None
    if isinstance(attachments, list):
        photos = [a for a in attachments if isinstance(a, dict)]
        if photos:
            lines.append(f"  Attachments: {len(photos)}")
    return "\n".join(lines)


def build_estimate_tools(service: AppFolioVendorService) -> list[Tool]:
    """Return the AppFolio estimate tools."""

    async def appfolio_get_estimate(estimate_id: str) -> ToolResult:
        try:
            payload = await service.get_estimate(estimate_id)
        except Exception as exc:
            return service_error_to_tool_result("fetching estimate", exc)
        if not payload:
            return ToolResult(
                content=f"Estimate {estimate_id} not found.",
                is_error=True,
                error_kind=ToolErrorKind.NOT_FOUND,
            )
        # If the JSON:API envelope is missing both ``data`` and the
        # core attributes (amount/description/status), log so we can
        # see the actual shape rather than rendering an empty card.
        if isinstance(payload, dict):
            data = payload.get("data") if "data" in payload else payload
            attrs: dict[str, Any] = (
                data.get("attributes")
                if isinstance(data, dict) and isinstance(data.get("attributes"), dict)
                else (data if isinstance(data, dict) else {})
            )
            if not any(attrs.get(k) for k in ("amount", "total", "description", "status")):
                log_unexpected_response_shape(
                    f"appfolio_get_estimate(estimate_id={estimate_id})",
                    payload,
                    expected=(
                        "JSON:API envelope with `data.attributes` containing "
                        "at least one of amount/total/description/status"
                    ),
                )
        return ToolResult(content=_fmt_estimate(payload))

    async def appfolio_update_estimate(
        estimate_id: str,
        amount: float | None = None,
        description: str = "",
        notes: str = "",
    ) -> ToolResult:
        attributes: dict[str, Any] = {}
        if amount is not None:
            attributes["amount"] = amount
        if description:
            attributes["description"] = description
        if notes:
            attributes["notes"] = notes
        if not attributes:
            return ToolResult(
                content="At least one of amount, description, or notes must be provided.",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )
        try:
            await service.update_estimate(estimate_id, attributes=attributes)
        except Exception as exc:
            return service_error_to_tool_result("updating estimate", exc)
        return ToolResult(
            content=f"Updated estimate {estimate_id}: {sorted(attributes.keys())}.",
            receipt=ToolReceipt(
                action="Updated AppFolio estimate",
                target=f"estimate {estimate_id}",
            ),
        )

    return [
        Tool(
            name=ToolName.APPFOLIO_GET_ESTIMATE,
            description="Get an AppFolio estimate (with attachments).",
            function=appfolio_get_estimate,
            params_model=AppFolioGetEstimateParams,
            usage_hint="Use when the user references a specific estimate by ID.",
        ),
        Tool(
            name=ToolName.APPFOLIO_UPDATE_ESTIMATE,
            description="Update an AppFolio estimate's amount, description, or notes.",
            function=appfolio_update_estimate,
            params_model=AppFolioUpdateEstimateParams,
            usage_hint=(
                "Confirm the new amount with the user before submitting;"
                " property managers see this estimate before approval."
            ),
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                description_builder=lambda args: (
                    f"Update AppFolio estimate {args.get('estimate_id', '?')}"
                ),
            ),
        ),
    ]
