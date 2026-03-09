"""Heartbeat checklist management tools for the agent."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from backend.app.agent.file_store import HeartbeatStore
from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.enums import ChecklistSchedule, ChecklistStatus

if TYPE_CHECKING:
    from backend.app.agent.tools.registry import ToolContext


class AddChecklistItemParams(BaseModel):
    """Parameters for the add_checklist_item tool."""

    description: str = Field(description="What to check or remind about")
    schedule: ChecklistSchedule = Field(
        default=ChecklistSchedule.DAILY,
        description="How often to check (default: daily)",
    )


class ListChecklistItemsParams(BaseModel):
    """Parameters for the list_checklist_items tool (no parameters)."""


class RemoveChecklistItemParams(BaseModel):
    """Parameters for the remove_checklist_item tool."""

    item_id: int = Field(description="ID of the checklist item to remove")


def create_checklist_tools(user_id: int) -> list[Tool]:
    """Create checklist-related tools for the agent."""

    async def add_checklist_item(
        description: str,
        schedule: str = ChecklistSchedule.DAILY,
    ) -> ToolResult:
        """Add an item to the user's heartbeat checklist."""
        if schedule not in list(ChecklistSchedule):
            return ToolResult(
                content=f"Invalid schedule '{schedule}'. Use: daily, weekdays, or once.",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )
        store = HeartbeatStore(user_id)
        item = await store.add_checklist_item(description=description, schedule=schedule)
        return ToolResult(content=f"Added to checklist (#{item.id}, {schedule}): {description}")

    async def list_checklist_items() -> ToolResult:
        """List all active checklist items."""
        store = HeartbeatStore(user_id)
        all_items = await store.get_checklist()
        items = [i for i in all_items if i.status == ChecklistStatus.ACTIVE]
        if not items:
            return ToolResult(content="No active checklist items.")
        lines = [f"- #{item.id}: {item.description} ({item.schedule})" for item in items]
        return ToolResult(content="\n".join(lines))

    async def remove_checklist_item(item_id: int) -> ToolResult:
        """Remove a checklist item by ID."""
        store = HeartbeatStore(user_id)
        deleted = await store.delete_checklist_item(item_id)
        if not deleted:
            return ToolResult(
                content=f"Checklist item #{item_id} not found.",
                is_error=True,
                error_kind=ToolErrorKind.NOT_FOUND,
            )
        return ToolResult(content=f"Removed checklist item #{item_id}")

    return [
        Tool(
            name=ToolName.ADD_CHECKLIST_ITEM,
            description=(
                "Add an item to the user's heartbeat checklist. "
                "The heartbeat will proactively check this item and remind "
                "the user when it's due."
            ),
            function=add_checklist_item,
            params_model=AddChecklistItemParams,
            usage_hint="When the user wants a recurring reminder, add it to the checklist.",
        ),
        Tool(
            name=ToolName.LIST_CHECKLIST_ITEMS,
            description="List all active items on the user's heartbeat checklist.",
            function=list_checklist_items,
            params_model=ListChecklistItemsParams,
            usage_hint="When asked about active reminders or checklist items, list them.",
        ),
        Tool(
            name=ToolName.REMOVE_CHECKLIST_ITEM,
            description="Remove an item from the user's heartbeat checklist by its ID.",
            function=remove_checklist_item,
            params_model=RemoveChecklistItemParams,
            usage_hint="When the user wants to stop a reminder, remove it by ID.",
        ),
    ]


def _checklist_factory(ctx: ToolContext) -> list[Tool]:
    """Factory for checklist tools, used by the registry."""
    return create_checklist_tools(ctx.user.id)


def _register() -> None:
    from backend.app.agent.tools.registry import default_registry

    default_registry.register(
        "checklist",
        _checklist_factory,
        core=False,
        summary="Manage recurring reminders and task checklists",
    )


_register()
