"""Heartbeat item management tools for the agent."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from backend.app.agent.file_store import HeartbeatStore
from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.enums import HeartbeatSchedule, HeartbeatStatus

if TYPE_CHECKING:
    from backend.app.agent.tools.registry import ToolContext


class AddHeartbeatItemParams(BaseModel):
    """Parameters for the add_heartbeat_item tool."""

    description: str = Field(description="What to check or remind about")
    schedule: HeartbeatSchedule = Field(
        default=HeartbeatSchedule.DAILY,
        description="How often to check (default: daily)",
    )


class ListHeartbeatItemsParams(BaseModel):
    """Parameters for the list_heartbeat_items tool (no parameters)."""


class RemoveHeartbeatItemParams(BaseModel):
    """Parameters for the remove_heartbeat_item tool."""

    item_id: str = Field(description="ID of the heartbeat item to remove")


def create_heartbeat_tools(user_id: str) -> list[Tool]:
    """Create heartbeat-related tools for the agent."""

    async def add_heartbeat_item(
        description: str,
        schedule: str = HeartbeatSchedule.DAILY,
    ) -> ToolResult:
        """Add an item to the user's heartbeat."""
        if schedule not in list(HeartbeatSchedule):
            return ToolResult(
                content=f"Invalid schedule '{schedule}'. Use: daily, weekdays, or once.",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )
        store = HeartbeatStore(user_id)
        item = await store.add_heartbeat_item(description=description, schedule=schedule)
        return ToolResult(content=f"Added to heartbeat (#{item.id}, {schedule}): {description}")

    async def list_heartbeat_items() -> ToolResult:
        """List all active heartbeat items."""
        store = HeartbeatStore(user_id)
        all_items = await store.get_heartbeat_items()
        items = [i for i in all_items if i.status == HeartbeatStatus.ACTIVE]
        if not items:
            return ToolResult(content="No active heartbeat items.")
        lines = [f"- #{item.id}: {item.description} ({item.schedule})" for item in items]
        return ToolResult(content="\n".join(lines))

    async def remove_heartbeat_item(item_id: str) -> ToolResult:
        """Remove a heartbeat item by ID."""
        store = HeartbeatStore(user_id)
        deleted = await store.delete_heartbeat_item(item_id)
        if not deleted:
            return ToolResult(
                content=f"Heartbeat item #{item_id} not found.",
                is_error=True,
                error_kind=ToolErrorKind.NOT_FOUND,
            )
        return ToolResult(content=f"Removed heartbeat item #{item_id}")

    return [
        Tool(
            name=ToolName.ADD_HEARTBEAT_ITEM,
            description=(
                "Add an item to the user's heartbeat. "
                "The heartbeat will proactively check this item and remind "
                "the user when it's due."
            ),
            function=add_heartbeat_item,
            params_model=AddHeartbeatItemParams,
            usage_hint="When the user wants a recurring reminder, add it to the heartbeat.",
        ),
        Tool(
            name=ToolName.LIST_HEARTBEAT_ITEMS,
            description="List all active items on the user's heartbeat.",
            function=list_heartbeat_items,
            params_model=ListHeartbeatItemsParams,
            usage_hint="When asked about active reminders or heartbeat items, list them.",
        ),
        Tool(
            name=ToolName.REMOVE_HEARTBEAT_ITEM,
            description="Remove an item from the user's heartbeat by its ID.",
            function=remove_heartbeat_item,
            params_model=RemoveHeartbeatItemParams,
            usage_hint="When the user wants to stop a reminder, remove it by ID.",
        ),
    ]


def _heartbeat_factory(ctx: ToolContext) -> list[Tool]:
    """Factory for heartbeat tools, used by the registry."""
    return create_heartbeat_tools(ctx.user.id)


def _register() -> None:
    from backend.app.agent.tools.registry import default_registry

    default_registry.register(
        "heartbeat",
        _heartbeat_factory,
        core=False,
        summary="Manage recurring reminders and task heartbeat items",
    )


_register()
