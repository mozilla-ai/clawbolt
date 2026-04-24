"""ServiceNow FSM tool registration and factory.

This module is the entrypoint for tool auto-discovery (the ``_tools``
suffix is picked up by ``ensure_tool_modules_imported``). It wires
together the implementation modules:

* ``servicenow_params``        -- Pydantic parameter models
* ``servicenow_work_orders``   -- work order and task tools
* ``servicenow_time``          -- time card tool

Authentication uses the standard OAuth 2.0 authorization code flow
(same as Google Calendar, QuickBooks, and CompanyCam).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from backend.app.agent.tools.base import Tool
from backend.app.agent.tools.names import ToolName
from backend.app.agent.tools.servicenow_time import build_time_tools
from backend.app.agent.tools.servicenow_work_orders import build_work_order_tools
from backend.app.config import settings
from backend.app.services.oauth import oauth_service
from backend.app.services.servicenow import ServiceNowService

if TYPE_CHECKING:
    from backend.app.agent.tools.registry import ToolContext

logger = logging.getLogger(__name__)

_INTEGRATION = "servicenow"


async def _load_service(user_id: str) -> ServiceNowService | None:
    """Load a ServiceNowService for the user using OAuth token (auto-refreshed).

    On first use after OAuth connection, resolves the user's ServiceNow
    sys_id lazily and persists it in the token's extra data so subsequent
    calls skip the resolution step.
    """
    token = await oauth_service.get_valid_token(user_id, _INTEGRATION)
    if not token or not token.access_token:
        return None

    instance_url = token.extra.get("instance_url", "") or settings.servicenow_instance_url
    if not instance_url:
        logger.warning("No ServiceNow instance URL for user %s", user_id)
        return None

    sys_user_id = token.extra.get("sys_user_id", "")

    try:
        service = ServiceNowService(
            access_token=token.access_token,
            instance_url=instance_url,
            sys_user_id=sys_user_id,
        )
    except ValueError:
        logger.exception("Invalid ServiceNow instance URL: %s", instance_url)
        return None

    # Lazy user identity resolution on first use.
    if not sys_user_id:
        try:
            resolved_id = await service.resolve_current_user()
            if resolved_id:
                service.sys_user_id = resolved_id
                token.extra["sys_user_id"] = resolved_id
                token.extra["instance_url"] = instance_url
                oauth_service.save_token(user_id, _INTEGRATION, token)
                logger.info(
                    "Resolved ServiceNow sys_user_id for user %s: %s",
                    user_id,
                    resolved_id,
                )
        except Exception:
            logger.warning(
                "Could not resolve ServiceNow user identity for %s; "
                "list_work_orders will not filter by default",
                user_id,
                exc_info=True,
            )

    return service


def _servicenow_auth_check(ctx: ToolContext) -> str | None:
    """Check whether ServiceNow FSM is available for this user.

    Returns None when connected (tools are available).
    Returns a reason string when not connected (tells the agent how to help).
    """
    if (
        not settings.servicenow_client_id
        or not settings.servicenow_client_secret
        or not settings.servicenow_instance_url
    ):
        return None  # Not configured server-side, hide tools
    if oauth_service.is_connected(ctx.user.id, _INTEGRATION):
        return None
    return (
        "ServiceNow FSM is not connected. "
        "Use manage_integration(action='connect', target='servicenow') "
        "to start the OAuth authorization flow."
    )


async def _servicenow_factory(ctx: ToolContext) -> list[Tool]:
    """Factory for ServiceNow FSM tools."""
    if (
        not settings.servicenow_client_id
        or not settings.servicenow_client_secret
        or not settings.servicenow_instance_url
    ):
        return []

    service = await _load_service(ctx.user.id)
    if service is None:
        return []
    return [
        *build_work_order_tools(service),
        *build_time_tools(service),
    ]


def _register() -> None:
    from backend.app.agent.tools.registry import SubToolInfo, default_registry

    default_registry.register(
        "servicenow",
        _servicenow_factory,
        core=False,
        summary=(
            "Manage ServiceNow Field Service Management work orders, tasks, "
            "notes, and time tracking"
        ),
        sub_tools=[
            SubToolInfo(
                ToolName.SERVICENOW_LIST_WORK_ORDERS,
                "List assigned work orders",
                default_permission="always",
            ),
            SubToolInfo(
                ToolName.SERVICENOW_GET_WORK_ORDER,
                "Get full work order details",
                default_permission="always",
            ),
            SubToolInfo(
                ToolName.SERVICENOW_LIST_TASKS,
                "List tasks for a work order",
                default_permission="always",
            ),
            SubToolInfo(
                ToolName.SERVICENOW_UPDATE_TASK,
                "Update a task state",
                default_permission="ask",
            ),
            SubToolInfo(
                ToolName.SERVICENOW_ADD_WORK_ORDER_NOTE,
                "Add a work note to a work order",
                default_permission="ask",
            ),
            SubToolInfo(
                ToolName.SERVICENOW_ADD_TASK_NOTE,
                "Add a work note to a task",
                default_permission="ask",
            ),
            SubToolInfo(
                ToolName.SERVICENOW_LOG_TIME,
                "Log time worked on a task",
                default_permission="ask",
            ),
            SubToolInfo(
                ToolName.SERVICENOW_SEARCH,
                "Search work orders by description or number",
                default_permission="always",
            ),
        ],
        auth_check=_servicenow_auth_check,
    )


_register()
