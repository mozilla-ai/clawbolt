"""Heartbeat checklist management tools for the agent."""

from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.app.agent.tools.base import Tool, ToolResult
from backend.app.agent.tools.registry import ToolContext, ToolFactory, default_registry
from backend.app.enums import ChecklistSchedule, ChecklistStatus
from backend.app.models import HeartbeatChecklistItem


class AddChecklistItemParams(BaseModel):
    """Parameters for the add_checklist_item tool."""

    description: str = Field(description="What to check or remind about")
    schedule: Literal["daily", "weekdays", "once"] = Field(
        default=ChecklistSchedule.DAILY,
        description="How often to check (default: daily)",
    )


class ListChecklistItemsParams(BaseModel):
    """Parameters for the list_checklist_items tool (no parameters)."""


class RemoveChecklistItemParams(BaseModel):
    """Parameters for the remove_checklist_item tool."""

    item_id: int = Field(description="ID of the checklist item to remove")


def create_checklist_tools(db: Session, contractor_id: int) -> list[Tool]:
    """Create checklist-related tools for the agent."""

    async def add_checklist_item(
        description: str,
        schedule: str = ChecklistSchedule.DAILY,
    ) -> ToolResult:
        """Add an item to the contractor's heartbeat checklist."""
        if schedule not in list(ChecklistSchedule):
            return ToolResult(
                content=f"Invalid schedule '{schedule}'. Use: daily, weekdays, or once.",
                is_error=True,
            )
        item = HeartbeatChecklistItem(
            contractor_id=contractor_id,
            description=description,
            schedule=schedule,
        )
        db.add(item)
        db.commit()
        db.refresh(item)
        return ToolResult(content=f"Added to checklist (#{item.id}, {schedule}): {description}")

    async def list_checklist_items() -> ToolResult:
        """List all active checklist items."""
        items = (
            db.query(HeartbeatChecklistItem)
            .filter(
                HeartbeatChecklistItem.contractor_id == contractor_id,
                HeartbeatChecklistItem.status == ChecklistStatus.ACTIVE,
            )
            .order_by(HeartbeatChecklistItem.id)
            .all()
        )
        if not items:
            return ToolResult(content="No active checklist items.")
        lines = [f"- #{item.id}: {item.description} ({item.schedule})" for item in items]
        return ToolResult(content="\n".join(lines))

    async def remove_checklist_item(item_id: int) -> ToolResult:
        """Remove a checklist item by ID."""
        item = (
            db.query(HeartbeatChecklistItem)
            .filter(
                HeartbeatChecklistItem.id == item_id,
                HeartbeatChecklistItem.contractor_id == contractor_id,
            )
            .first()
        )
        if not item:
            return ToolResult(content=f"Checklist item #{item_id} not found.", is_error=True)
        db.delete(item)
        db.commit()
        return ToolResult(content=f"Removed checklist item #{item_id}: {item.description}")

    return [
        Tool(
            name="add_checklist_item",
            description=(
                "Add an item to the contractor's heartbeat checklist. "
                "The heartbeat will proactively check this item and remind "
                "the contractor when it's due."
            ),
            function=add_checklist_item,
            params_model=AddChecklistItemParams,
        ),
        Tool(
            name="list_checklist_items",
            description="List all active items on the contractor's heartbeat checklist.",
            function=list_checklist_items,
            params_model=ListChecklistItemsParams,
        ),
        Tool(
            name="remove_checklist_item",
            description="Remove an item from the contractor's heartbeat checklist by its ID.",
            function=remove_checklist_item,
            params_model=RemoveChecklistItemParams,
        ),
    ]


def _factory(ctx: ToolContext) -> list[Tool]:
    return create_checklist_tools(ctx.db, ctx.contractor.id)


default_registry.register(ToolFactory(create=_factory))
