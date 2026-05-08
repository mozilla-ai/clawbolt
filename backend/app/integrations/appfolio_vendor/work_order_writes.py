"""Write tools for AppFolio work orders.

Accept, schedule, status update, and undo. Notes (with photo upload)
live in :mod:`notes`; tenant messaging lives in :mod:`conversations`.
"""

from __future__ import annotations

import logging

from backend.app.agent.approval import ApprovalPolicy, PermissionLevel
from backend.app.agent.tools.base import Tool, ToolReceipt, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.integrations.appfolio_vendor.errors import service_error_to_tool_result
from backend.app.integrations.appfolio_vendor.params import (
    AppFolioAcceptWorkOrderParams,
    AppFolioScheduleWorkOrderParams,
    AppFolioUndoWorkOrderStatusParams,
    AppFolioUpdateWorkOrderStatusParams,
)
from backend.app.integrations.appfolio_vendor.service import AppFolioVendorService

logger = logging.getLogger(__name__)


def build_work_order_write_tools(service: AppFolioVendorService) -> list[Tool]:
    """Return the work-order write Tool instances."""

    async def appfolio_accept_work_order(work_order_id: str, notes: str = "") -> ToolResult:
        # When the user supplies no notes, send no body at all rather than
        # an empty dict; mirrors the SPA which only POSTs a body when the
        # user filled in the optional notes field.
        body = {"notes": notes} if notes else None
        try:
            await service.accept_work_order(work_order_id, body=body)
        except Exception as exc:
            return service_error_to_tool_result("accepting work order", exc)
        return ToolResult(
            content=f"Accepted work order {work_order_id}.",
            receipt=ToolReceipt(
                action="Accepted AppFolio work order",
                target=f"#{work_order_id}",
            ),
        )

    async def appfolio_schedule_work_order(
        work_order_id: str,
        time_slot_id: str,
    ) -> ToolResult:
        try:
            await service.schedule_work_order(work_order_id, time_slot_id=time_slot_id)
        except Exception as exc:
            return service_error_to_tool_result("scheduling work order", exc)
        return ToolResult(
            content=f"Scheduled work order {work_order_id} (slot {time_slot_id}).",
            receipt=ToolReceipt(
                action="Scheduled AppFolio work order",
                target=f"#{work_order_id} slot {time_slot_id}",
            ),
        )

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
            content=(f"Reverted work order {work_order_id} status to {previous_status}."),
            receipt=ToolReceipt(
                action="Reverted AppFolio work order status",
                target=f"#{work_order_id} → {previous_status}",
            ),
        )

    return [
        Tool(
            name=ToolName.APPFOLIO_ACCEPT_WORK_ORDER,
            description="Accept an AppFolio work order assignment.",
            function=appfolio_accept_work_order,
            params_model=AppFolioAcceptWorkOrderParams,
            usage_hint=(
                "Use when the user agrees to take a job. Optional notes are"
                " visible to the property manager."
            ),
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                description_builder=lambda args: (
                    f"Accept AppFolio work order #{args.get('work_order_id', '?')}"
                ),
            ),
        ),
        Tool(
            name=ToolName.APPFOLIO_SCHEDULE_WORK_ORDER,
            description="Set the scheduled visit time on an AppFolio work order.",
            function=appfolio_schedule_work_order,
            params_model=AppFolioScheduleWorkOrderParams,
            usage_hint=(
                "Confirm the date, time, and timezone with the user before"
                " calling. AppFolio sends the tenant a 24-hour reminder."
            ),
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                description_builder=lambda args: (
                    f"Schedule AppFolio work order #{args.get('work_order_id', '?')}"
                    f" (slot {args.get('time_slot_id', '?')})"
                ),
            ),
        ),
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
