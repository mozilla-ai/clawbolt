"""ServiceNow FSM work order and task tools.

Implements list, get, search, update, and note operations for work orders
and work order tasks. Built by the factory in ``servicenow_tools``.
"""

from __future__ import annotations

import logging

import httpx

from backend.app.agent.approval import ApprovalPolicy, PermissionLevel
from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolReceipt, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.agent.tools.servicenow_params import (
    AddTaskNoteParams,
    AddWorkOrderNoteParams,
    GetWorkOrderParams,
    ListTasksParams,
    ListWorkOrdersParams,
    SearchParams,
    UpdateTaskParams,
)
from backend.app.services.servicenow import ServiceNowService
from backend.app.services.servicenow_models import WorkOrder, WorkOrderTask

logger = logging.getLogger(__name__)


def _wo_url(service: ServiceNowService, sys_id: str) -> str:
    """Build a deep link to a work order in the ServiceNow UI."""
    return f"{service._instance_url}/wm_order.do?sys_id={sys_id}"


def _task_url(service: ServiceNowService, sys_id: str) -> str:
    """Build a deep link to a work order task in the ServiceNow UI."""
    return f"{service._instance_url}/wm_task.do?sys_id={sys_id}"


def _format_work_order(wo: WorkOrder) -> str:
    """Format a work order for display."""
    lines = [
        f"**{wo.number}**: {wo.short_description}",
        f"  State: {wo.state}",
        f"  Priority: {wo.priority}",
        f"  Assigned to: {wo.assigned_to}",
    ]
    if str(wo.location):
        lines.append(f"  Location: {wo.location}")
    if wo.opened_at:
        lines.append(f"  Opened: {wo.opened_at}")
    return "\n".join(lines)


def _format_task(task: WorkOrderTask) -> str:
    """Format a work order task for display."""
    lines = [
        f"**{task.number}**: {task.short_description}",
        f"  State: {task.state}",
        f"  Assigned to: {task.assigned_to}",
    ]
    if str(task.work_order):
        lines.append(f"  Work order: {task.work_order}")
    return "\n".join(lines)


def build_work_order_tools(service: ServiceNowService) -> list[Tool]:
    """Build all work order and task tools for the ServiceNow integration."""

    async def servicenow_list_work_orders(
        assigned_to: str = "",
        state: str = "",
        limit: int = 25,
    ) -> ToolResult:
        try:
            orders = await service.list_work_orders(
                assigned_to=assigned_to,
                state=state,
                limit=limit,
            )
            if not orders:
                return ToolResult(content="No work orders found.")
            lines = [f"Found {len(orders)} work order(s):\n"]
            for wo in orders:
                lines.append(_format_work_order(wo))
                lines.append("")
            return ToolResult(content="\n".join(lines))
        except Exception as exc:
            logger.exception("Failed to list work orders")
            return ToolResult(
                content=f"Failed to list work orders: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

    async def servicenow_get_work_order(sys_id: str) -> ToolResult:
        try:
            wo = await service.get_work_order(sys_id)
            lines = [_format_work_order(wo)]
            if wo.description:
                lines.append(f"\nDescription:\n{wo.description}")
            return ToolResult(content="\n".join(lines))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return ToolResult(
                    content=f"Work order not found: {sys_id}",
                    is_error=True,
                    error_kind=ToolErrorKind.NOT_FOUND,
                )
            return ToolResult(
                content=f"Failed to get work order: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )
        except Exception as exc:
            logger.exception("Failed to get work order")
            return ToolResult(
                content=f"Failed to get work order: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

    async def servicenow_list_tasks(
        work_order_id: str = "",
        state: str = "",
        limit: int = 25,
    ) -> ToolResult:
        try:
            tasks = await service.list_tasks(
                work_order_id=work_order_id,
                state=state,
                limit=limit,
            )
            if not tasks:
                return ToolResult(content="No tasks found.")
            lines = [f"Found {len(tasks)} task(s):\n"]
            for task in tasks:
                lines.append(_format_task(task))
                lines.append("")
            return ToolResult(content="\n".join(lines))
        except Exception as exc:
            logger.exception("Failed to list tasks")
            return ToolResult(
                content=f"Failed to list tasks: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

    async def servicenow_update_task(
        sys_id: str,
        state: str | None = None,
        work_notes: str = "",
    ) -> ToolResult:
        try:
            task = await service.update_task(
                sys_id,
                state=state or "",
                work_notes=work_notes,
            )
            return ToolResult(
                content=f"Updated task {task.number}: state={task.state}",
                receipt=ToolReceipt(
                    action="Updated task status",
                    target=task.number or sys_id,
                    url=_task_url(service, sys_id),
                ),
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return ToolResult(
                    content=f"Task not found: {sys_id}",
                    is_error=True,
                    error_kind=ToolErrorKind.NOT_FOUND,
                )
            return ToolResult(
                content=f"Failed to update task: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )
        except Exception as exc:
            logger.exception("Failed to update task")
            return ToolResult(
                content=f"Failed to update task: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

    async def servicenow_add_work_order_note(
        sys_id: str,
        note: str,
    ) -> ToolResult:
        try:
            wo = await service.add_work_order_note(sys_id, note)
            return ToolResult(
                content=f"Added work note to {wo.number}.",
                receipt=ToolReceipt(
                    action="Added work note",
                    target=wo.number or sys_id,
                    url=_wo_url(service, sys_id),
                ),
            )
        except Exception as exc:
            logger.exception("Failed to add work order note")
            return ToolResult(
                content=f"Failed to add work note: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

    async def servicenow_add_task_note(
        sys_id: str,
        note: str,
    ) -> ToolResult:
        try:
            task = await service.add_task_note(sys_id, note)
            return ToolResult(
                content=f"Added work note to {task.number}.",
                receipt=ToolReceipt(
                    action="Added work note",
                    target=task.number or sys_id,
                    url=_task_url(service, sys_id),
                ),
            )
        except Exception as exc:
            logger.exception("Failed to add task note")
            return ToolResult(
                content=f"Failed to add task note: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

    async def servicenow_search(
        query: str,
        limit: int = 25,
    ) -> ToolResult:
        try:
            orders = await service.search_work_orders(query, limit=limit)
            if not orders:
                return ToolResult(content=f"No work orders found matching '{query}'.")
            lines = [f"Found {len(orders)} work order(s) matching '{query}':\n"]
            for wo in orders:
                lines.append(_format_work_order(wo))
                lines.append("")
            return ToolResult(content="\n".join(lines))
        except Exception as exc:
            logger.exception("Failed to search work orders")
            return ToolResult(
                content=f"Failed to search work orders: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

    return [
        Tool(
            name=ToolName.SERVICENOW_LIST_WORK_ORDERS,
            description="List ServiceNow work orders assigned to the current user or a specific user.",
            function=servicenow_list_work_orders,
            params_model=ListWorkOrdersParams,
            usage_hint="Use to check what work is assigned. Defaults to the current user.",
        ),
        Tool(
            name=ToolName.SERVICENOW_GET_WORK_ORDER,
            description="Get full details of a specific ServiceNow work order.",
            function=servicenow_get_work_order,
            params_model=GetWorkOrderParams,
            usage_hint="Use after listing work orders to get full details including description.",
        ),
        Tool(
            name=ToolName.SERVICENOW_LIST_TASKS,
            description="List tasks for a ServiceNow work order.",
            function=servicenow_list_tasks,
            params_model=ListTasksParams,
            usage_hint="Use to see the individual steps within a work order.",
        ),
        Tool(
            name=ToolName.SERVICENOW_UPDATE_TASK,
            description="Update a ServiceNow work order task state or add a work note.",
            function=servicenow_update_task,
            params_model=UpdateTaskParams,
            usage_hint="Use to change task status (e.g. Accept, Work In Progress, Closed Complete).",
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                description_builder=lambda args: (
                    "Update ServiceNow task"
                    + (f" to '{args.get('state')}'" if args.get("state") else "")
                ),
            ),
        ),
        Tool(
            name=ToolName.SERVICENOW_ADD_WORK_ORDER_NOTE,
            description="Add a work note to a ServiceNow work order.",
            function=servicenow_add_work_order_note,
            params_model=AddWorkOrderNoteParams,
            usage_hint="Use to log notes visible to the dispatcher and team.",
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                description_builder=lambda args: "Add work note to ServiceNow work order",
            ),
        ),
        Tool(
            name=ToolName.SERVICENOW_ADD_TASK_NOTE,
            description="Add a work note to a ServiceNow work order task.",
            function=servicenow_add_task_note,
            params_model=AddTaskNoteParams,
            usage_hint="Use to log notes on a specific task step.",
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                description_builder=lambda args: "Add work note to ServiceNow task",
            ),
        ),
        Tool(
            name=ToolName.SERVICENOW_SEARCH,
            description="Search ServiceNow work orders by description or number.",
            function=servicenow_search,
            params_model=SearchParams,
            usage_hint="Use to find work orders by keyword. Searches descriptions and numbers.",
        ),
    ]
