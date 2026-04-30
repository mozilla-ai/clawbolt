"""Integration management tool for chat-based control.

Gives the agent the ability to view integration status, enable/disable
tool groups, and connect/disconnect OAuth integrations, so users can
manage everything over chat without needing the web UI.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from backend.app.agent.stores import ToolConfigStore
from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.services.oauth import get_oauth_config, list_oauth_integrations, oauth_service

if TYPE_CHECKING:
    from backend.app.agent.tools.registry import ToolContext, ToolRegistry

logger = logging.getLogger(__name__)

# Human-readable display names for tool groups.
_DISPLAY_NAMES: dict[str, str] = {
    "workspace": "Workspace",
    "profile": "Profile",
    "memory": "Memory",
    "messaging": "Messaging",
    "file": "File management",
    "heartbeat": "Heartbeat",
    "quickbooks": "QuickBooks Online",
    "calendar": "Google Calendar",
    "companycam": "CompanyCam",
    "supplier_pricing": "Home Depot pricing",
}

# Map tool group names to their OAuth integration names.
_TOOL_OAUTH_MAP: dict[str, str] = {
    "calendar": "google_calendar",
    "quickbooks": "quickbooks",
    "companycam": "companycam",
}


class ManageIntegrationParams(BaseModel):
    """Parameters for the manage_integration tool."""

    action: Literal["status", "enable", "disable", "connect", "disconnect"] = Field(
        description=(
            "Action to perform: "
            "'status' to list all integrations and their state, "
            "'enable' or 'disable' to toggle a tool group, "
            "'connect' to get an OAuth link for an integration, "
            "'disconnect' to remove an OAuth connection."
        ),
    )
    target: str | None = Field(
        default=None,
        description=(
            "Tool group name (for enable/disable) or OAuth integration name "
            "(for connect/disconnect). Not needed for status."
        ),
    )


def create_integration_tools(ctx: ToolContext) -> list[Tool]:
    """Create the manage_integration tool scoped to the current user."""
    from backend.app.agent.tools.registry import default_registry, ensure_tool_modules_imported

    ensure_tool_modules_imported()

    user_id = ctx.user.id

    async def manage_integration(
        action: str,
        target: str | None = None,
    ) -> ToolResult:
        if action == "status":
            return await _handle_status(user_id, default_registry)

        if target is None:
            return ToolResult(
                content=f"The '{action}' action requires a target. Specify a tool group name.",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )

        if action == "enable":
            return await _handle_enable(user_id, target, default_registry)
        if action == "disable":
            return await _handle_disable(user_id, target, default_registry)
        if action == "connect":
            return _handle_connect(user_id, target)
        if action == "disconnect":
            return _handle_disconnect(user_id, target)

        valid_actions = "status, enable, disable, connect, disconnect"
        return ToolResult(
            content=f"Unknown action '{action}'. Valid actions: {valid_actions}",
            is_error=True,
            error_kind=ToolErrorKind.VALIDATION,
        )

    return [
        Tool(
            name=ToolName.MANAGE_INTEGRATION,
            description=(
                "Manage integrations: view status, enable/disable tool groups, "
                "connect/disconnect OAuth integrations. "
                "Use this when the user asks about their integrations or wants to "
                "change what tools are available."
            ),
            function=manage_integration,
            params_model=ManageIntegrationParams,
            usage_hint=(
                "Use manage_integration to help users control their integrations. "
                "Before offering ANY connect link, call action='status' first and "
                "skip integrations already showing as connected (do not re-prompt "
                "for something they already set up). "
                "Call with action='connect' and target='google_calendar' or 'quickbooks' "
                "to generate an OAuth link the user can tap to connect. "
                "Call with action='enable'/'disable' and target=group_name to toggle tools."
            ),
        ),
    ]


async def _handle_status(
    user_id: str,
    registry: ToolRegistry,
) -> ToolResult:
    """Build a status overview of all tool groups."""
    store = ToolConfigStore(user_id)
    disabled_groups = await store.get_disabled_tool_names()

    core_lines: list[str] = []
    integration_lines: list[str] = []

    for name in sorted(registry.factory_names):
        factory = registry._factories.get(name)
        if factory is None:
            continue

        display = _DISPLAY_NAMES.get(name, name)

        if factory.core:
            core_lines.append(f"- {name}: {display} (always enabled)")
        else:
            enabled = name not in disabled_groups
            status_parts: list[str] = ["enabled" if enabled else "disabled"]

            # Check OAuth connection status
            oauth_name = _TOOL_OAUTH_MAP.get(name)
            if oauth_name:
                config = get_oauth_config(oauth_name)
                if config is not None and config.is_configured:
                    connected = oauth_service.is_connected(user_id, oauth_name)
                    status_parts.append("connected" if connected else "not connected")
                else:
                    status_parts.append("not configured by admin")

            integration_lines.append(f"- {name}: {display} ({', '.join(status_parts)})")

    lines: list[str] = []
    if core_lines:
        lines.append("Core tools:")
        lines.extend(core_lines)
    if integration_lines:
        if lines:
            lines.append("")
        lines.append("Integrations:")
        lines.extend(integration_lines)

    if not lines:
        return ToolResult(content="No tool groups registered.")

    return ToolResult(content="\n".join(lines))


async def _handle_enable(
    user_id: str,
    target: str,
    registry: ToolRegistry,
) -> ToolResult:
    """Enable a tool group."""
    if target not in registry.factory_names:
        available = [
            n for n in sorted(registry.factory_names) if n not in registry.core_factory_names
        ]
        return ToolResult(
            content=(
                f"Unknown tool group '{target}'. Available integrations: {', '.join(available)}"
            ),
            is_error=True,
            error_kind=ToolErrorKind.NOT_FOUND,
        )

    factory = registry._factories.get(target)
    if factory and factory.core:
        display = _DISPLAY_NAMES.get(target, target)
        return ToolResult(
            content=f"{display} is a core tool and is always enabled.",
        )

    store = ToolConfigStore(user_id)
    await store.set_enabled(target, enabled=True)

    display = _DISPLAY_NAMES.get(target, target)
    logger.info("User %s enabled tool group '%s' via chat", user_id, target)
    return ToolResult(
        content=f"Enabled {display} tools. They will be available starting with your next message.",
    )


async def _handle_disable(
    user_id: str,
    target: str,
    registry: ToolRegistry,
) -> ToolResult:
    """Disable a tool group."""
    if target not in registry.factory_names:
        available = [
            n for n in sorted(registry.factory_names) if n not in registry.core_factory_names
        ]
        return ToolResult(
            content=(
                f"Unknown tool group '{target}'. Available integrations: {', '.join(available)}"
            ),
            is_error=True,
            error_kind=ToolErrorKind.NOT_FOUND,
        )

    factory = registry._factories.get(target)
    if factory and factory.core:
        display = _DISPLAY_NAMES.get(target, target)
        return ToolResult(
            content=f"{display} is a core tool and cannot be disabled.",
            is_error=True,
            error_kind=ToolErrorKind.VALIDATION,
        )

    store = ToolConfigStore(user_id)
    await store.set_enabled(target, enabled=False)

    display = _DISPLAY_NAMES.get(target, target)
    logger.info("User %s disabled tool group '%s' via chat", user_id, target)
    return ToolResult(
        content=f"Disabled {display} tools. This takes effect starting with your next message.",
    )


def _handle_connect(user_id: str, target: str) -> ToolResult:
    """Generate an OAuth authorization URL for an integration."""
    # Check if target is a tool group name that maps to an OAuth integration
    oauth_name = _TOOL_OAUTH_MAP.get(target, target)

    if oauth_name not in list_oauth_integrations():
        return ToolResult(
            content=(
                f"'{target}' does not use OAuth authentication. "
                f"OAuth integrations: {', '.join(list_oauth_integrations())}"
            ),
            is_error=True,
            error_kind=ToolErrorKind.VALIDATION,
        )

    config = get_oauth_config(oauth_name)
    if config is None or not config.is_configured:
        display = _DISPLAY_NAMES.get(target, target)
        return ToolResult(
            content=(
                f"{display} is not configured. "
                "The admin needs to set up the integration credentials first."
            ),
            is_error=True,
            error_kind=ToolErrorKind.AUTH,
        )

    if oauth_service.is_connected(user_id, oauth_name):
        display = _DISPLAY_NAMES.get(target, target)
        return ToolResult(
            content=f"{display} is already connected. Use action='disconnect' first to reconnect.",
        )

    url = oauth_service.get_authorization_url(config, user_id, source="chat")
    display = _DISPLAY_NAMES.get(target, target)
    logger.info("User %s requested OAuth connect link for '%s' via chat", user_id, oauth_name)
    return ToolResult(
        content=(
            f"To connect {display}, open this link:\n\n{url}\n\n"
            "After you approve access, the connection will be ready "
            "the next time you message me."
        ),
    )


def _handle_disconnect(user_id: str, target: str) -> ToolResult:
    """Remove OAuth tokens for an integration."""
    oauth_name = _TOOL_OAUTH_MAP.get(target, target)

    if oauth_name not in list_oauth_integrations():
        return ToolResult(
            content=(
                f"'{target}' does not use OAuth authentication. "
                f"OAuth integrations: {', '.join(list_oauth_integrations())}"
            ),
            is_error=True,
            error_kind=ToolErrorKind.VALIDATION,
        )

    if not oauth_service.is_connected(user_id, oauth_name):
        display = _DISPLAY_NAMES.get(target, target)
        return ToolResult(
            content=f"{display} is not currently connected.",
            is_error=True,
            error_kind=ToolErrorKind.NOT_FOUND,
        )

    oauth_service.delete_token(user_id, oauth_name)
    display = _DISPLAY_NAMES.get(target, target)
    logger.info("User %s disconnected OAuth for '%s' via chat", user_id, oauth_name)
    return ToolResult(
        content=(
            f"Disconnected {display}. "
            "The tools are still enabled but won't work until you reconnect."
        ),
    )


def _integration_factory(ctx: ToolContext) -> list[Tool]:
    """Factory for integration management tools, used by the registry."""
    return create_integration_tools(ctx)


def _register() -> None:
    from backend.app.agent.tools.registry import SubToolInfo, default_registry

    default_registry.register(
        "integration",
        _integration_factory,
        core=True,
        sub_tools=[
            SubToolInfo(
                ToolName.MANAGE_INTEGRATION,
                "View status, enable/disable tools, connect/disconnect integrations",
            ),
        ],
    )


_register()
