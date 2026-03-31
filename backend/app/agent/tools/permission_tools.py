"""Tool for users to change tool permission levels via chat."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from backend.app.agent.approval import (
    ApprovalPolicy,
    PermissionLevel,
    get_approval_store,
)
from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolResult
from backend.app.agent.tools.names import ToolName

if TYPE_CHECKING:
    from backend.app.agent.tools.registry import ToolContext

logger = logging.getLogger(__name__)

_VALID_LEVELS = {"auto", "ask", "deny"}

# Human-friendly aliases mapped to PermissionLevel values.
_LEVEL_ALIASES: dict[str, str] = {
    "auto": "auto",
    "ask": "ask",
    "deny": "deny",
    "always": "auto",
    "never": "deny",
    "block": "deny",
    "blocked": "deny",
    "allow": "auto",
    "freely": "auto",
}


class UpdatePermissionParams(BaseModel):
    """Parameters for the update_permission tool."""

    tool_name: str = Field(
        description=(
            "The name of the tool to update. Examples: send_reply, "
            "send_media_reply, calendar_create_event, qb_create, delete_file."
        ),
    )
    permission: str = Field(
        description=(
            "The new permission level. Use 'auto' to let the tool run freely, "
            "'ask' to require approval each time, or 'deny' to block it entirely."
        ),
    )


def create_permission_tools(user_id: str) -> list[Tool]:
    """Create permission management tools for the agent."""

    async def update_permission(tool_name: str, permission: str) -> ToolResult:
        """Change the permission level for a tool."""
        normalized = permission.strip().lower()
        level_str = _LEVEL_ALIASES.get(normalized)
        if level_str is None:
            return ToolResult(
                content=(
                    f"Unknown permission '{permission}'. "
                    f"Use 'auto' (run freely), 'ask' (ask first), or 'deny' (block)."
                ),
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )

        level = PermissionLevel(level_str)
        store = get_approval_store()
        store.set_permission(user_id, tool_name, level)

        labels = {"auto": "run freely", "ask": "ask first", "deny": "blocked"}
        label = labels.get(level_str, level_str)
        logger.info(
            "Permission updated: user=%s tool=%s level=%s",
            user_id,
            tool_name,
            level_str,
        )
        return ToolResult(content=f"Done. {tool_name} is now set to: {label}.")

    return [
        Tool(
            name=ToolName.UPDATE_PERMISSION,
            description=(
                "Change whether a tool runs freely, asks for approval, or is blocked. "
                "Use this when the user wants to change how the assistant handles "
                "a specific action, like sending messages or creating events."
            ),
            function=update_permission,
            params_model=UpdatePermissionParams,
            usage_hint=(
                "Use when the user asks to change permissions, e.g. "
                "'always ask before sending messages' or 'stop blocking calendar events'."
            ),
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.AUTO,
                description_builder=lambda args: (
                    f"Update permission for {args.get('tool_name', 'tool')}"
                ),
            ),
        ),
    ]


def _permission_factory(ctx: ToolContext) -> list[Tool]:
    """Factory for permission tools, used by the registry."""
    return create_permission_tools(ctx.user.id)


def _register() -> None:
    from backend.app.agent.tools.registry import SubToolInfo, default_registry

    default_registry.register(
        "permissions",
        _permission_factory,
        core=True,
        summary="Change tool permission levels (run freely, ask first, or block)",
        sub_tools=[
            SubToolInfo(
                ToolName.UPDATE_PERMISSION,
                "Change whether a tool runs freely or asks first",
            ),
        ],
    )


_register()
