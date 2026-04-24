"""ServiceNow FSM time card tool.

Implements time logging for work order tasks. Built by the factory in
``servicenow_tools``.
"""

from __future__ import annotations

import logging

from backend.app.agent.approval import ApprovalPolicy, PermissionLevel
from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolReceipt, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.agent.tools.servicenow_params import LogTimeParams
from backend.app.services.servicenow import ServiceNowService

logger = logging.getLogger(__name__)


def build_time_tools(service: ServiceNowService) -> list[Tool]:
    """Build the time logging tool for the ServiceNow integration."""

    async def servicenow_log_time(
        task_id: str,
        hours: float,
        date: str,
        category: str = "labor",
    ) -> ToolResult:
        try:
            card = await service.create_time_card(
                task_id=task_id,
                hours=hours,
                date=date,
                category=category,
            )
            return ToolResult(
                content=(f"Logged {hours}h ({category}) on {date} for task {card.task}."),
                receipt=ToolReceipt(
                    action="Logged time",
                    target=f"{hours}h {category} on {date}",
                    url=(f"{service._instance_url}/time_card.do?sys_id={card.sys_id}"),
                ),
            )
        except Exception as exc:
            logger.exception("Failed to log time")
            return ToolResult(
                content=f"Failed to log time: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

    return [
        Tool(
            name=ToolName.SERVICENOW_LOG_TIME,
            description="Log time worked on a ServiceNow work order task.",
            function=servicenow_log_time,
            params_model=LogTimeParams,
            usage_hint="Use to record hours worked. Date format: YYYY-MM-DD.",
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                description_builder=lambda args: (
                    f"Log {args.get('hours', '?')}h to ServiceNow task"
                ),
            ),
        ),
    ]
