"""Write tools for AppFolio work-order status.

Limited surface: status update and undo. Note writes live in
:mod:`notes`; invoice writes in :mod:`invoices`. The accept/schedule
and tenant-messaging tools that previously lived here were dropped in
#1331 ("trim tool surface to reads, notes, and invoices") and are not
restored.

Brought back the status tools after vendor feedback: marking a work
order complete from inside the assistant is the one write the trim
left without a workaround, and users now have to flip status in the
AppFolio UI by hand.
"""

from __future__ import annotations

import logging

from backend.app.agent.approval import ApprovalPolicy, PermissionLevel
from backend.app.agent.tools.base import Tool, ToolReceipt, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.integrations.appfolio_vendor.errors import service_error_to_tool_result
from backend.app.integrations.appfolio_vendor.params import (
    AppFolioUndoWorkOrderStatusParams,
    AppFolioUpdateWorkOrderStatusParams,
)
from backend.app.integrations.appfolio_vendor.service import AppFolioVendorService

logger = logging.getLogger(__name__)


def build_work_order_write_tools(service: AppFolioVendorService) -> list[Tool]:
    """Return the work-order status-write Tool instances."""

    async def appfolio_update_work_order_status(work_order_id: str, status_code: int) -> ToolResult:
        try:
            await service.update_work_order_status(work_order_id, status_code=status_code)
        except Exception as exc:
            return service_error_to_tool_result("updating work order status", exc)
        return ToolResult(
            content=f"Updated work order {work_order_id} status to {status_code}.",
            receipt=ToolReceipt(
                action="Updated AppFolio work order status",
                target=f"#{work_order_id} → {status_code}",
            ),
        )

    async def appfolio_undo_work_order_status(
        work_order_id: str, previous_status: str
    ) -> ToolResult:
        # AppFolio accepts either the int code or the string label; pass
        # whichever the agent supplied through unchanged so the API-side
        # validation is the canonical check rather than us guessing.
        try:
            await service.undo_work_order_status(work_order_id, previous_status=previous_status)
        except Exception as exc:
            return service_error_to_tool_result("undoing work order status", exc)
        return ToolResult(
            content=f"Reverted work order {work_order_id} status to {previous_status}.",
            receipt=ToolReceipt(
                action="Reverted AppFolio work order status",
                target=f"#{work_order_id} → {previous_status}",
            ),
        )

    return [
        Tool(
            name=ToolName.APPFOLIO_UPDATE_WORK_ORDER_STATUS,
            description="Update the status code on an AppFolio work order.",
            function=appfolio_update_work_order_status,
            params_model=AppFolioUpdateWorkOrderStatusParams,
            usage_hint=(
                "Use to mark a job in-progress, completed, or back to needs-action."
                " Confirm the target status with the user when uncertain."
            ),
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                description_builder=lambda args: (
                    f"Update AppFolio work order #{args.get('work_order_id', '?')}"
                    f" status to {args.get('status_code', '?')}"
                ),
            ),
        ),
        Tool(
            name=ToolName.APPFOLIO_UNDO_WORK_ORDER_STATUS,
            description="Revert a recent status change on an AppFolio work order.",
            function=appfolio_undo_work_order_status,
            params_model=AppFolioUndoWorkOrderStatusParams,
            usage_hint=(
                "Use only when the user explicitly asks to undo a status change they just made."
            ),
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                description_builder=lambda args: (
                    f"Revert AppFolio work order #{args.get('work_order_id', '?')}"
                    f" status to {args.get('previous_status', '?')}"
                ),
            ),
        ),
    ]
