"""Heartbeat checklist management tools for the agent."""

from sqlalchemy.orm import Session

from backend.app.agent.tools.base import Tool
from backend.app.models import HeartbeatChecklistItem


def create_checklist_tools(db: Session, contractor_id: int) -> list[Tool]:
    """Create checklist-related tools for the agent."""

    async def add_checklist_item(
        description: str,
        schedule: str = "daily",
    ) -> str:
        """Add an item to the contractor's heartbeat checklist."""
        if schedule not in ("daily", "weekdays", "once"):
            return f"Invalid schedule '{schedule}'. Use: daily, weekdays, or once."
        item = HeartbeatChecklistItem(
            contractor_id=contractor_id,
            description=description,
            schedule=schedule,
        )
        db.add(item)
        db.commit()
        db.refresh(item)
        return f"Added to checklist (#{item.id}, {schedule}): {description}"

    async def list_checklist_items() -> str:
        """List all active checklist items."""
        items = (
            db.query(HeartbeatChecklistItem)
            .filter(
                HeartbeatChecklistItem.contractor_id == contractor_id,
                HeartbeatChecklistItem.status == "active",
            )
            .order_by(HeartbeatChecklistItem.id)
            .all()
        )
        if not items:
            return "No active checklist items."
        lines = [f"- #{item.id}: {item.description} ({item.schedule})" for item in items]
        return "\n".join(lines)

    async def remove_checklist_item(item_id: int) -> str:
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
            return f"Checklist item #{item_id} not found."
        db.delete(item)
        db.commit()
        return f"Removed checklist item #{item_id}: {item.description}"

    return [
        Tool(
            name="add_checklist_item",
            description=(
                "Add an item to the contractor's heartbeat checklist. "
                "The heartbeat will proactively check this item and remind "
                "the contractor when it's due."
            ),
            function=add_checklist_item,
            parameters={
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "What to check or remind about",
                    },
                    "schedule": {
                        "type": "string",
                        "enum": ["daily", "weekdays", "once"],
                        "description": "How often to check (default: daily)",
                    },
                },
                "required": ["description"],
            },
        ),
        Tool(
            name="list_checklist_items",
            description="List all active items on the contractor's heartbeat checklist.",
            function=list_checklist_items,
            parameters={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="remove_checklist_item",
            description="Remove an item from the contractor's heartbeat checklist by its ID.",
            function=remove_checklist_item,
            parameters={
                "type": "object",
                "properties": {
                    "item_id": {
                        "type": "integer",
                        "description": "ID of the checklist item to remove",
                    },
                },
                "required": ["item_id"],
            },
        ),
    ]
