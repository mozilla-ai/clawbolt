"""Profile update tool for the agent.

Provides a dedicated update_profile tool with explicit typed fields,
replacing the fragile fuzzy-matching approach that tried to infer
profile fields from save_fact keys.
"""

import json
import logging
import re

from sqlalchemy.orm import Session

from backend.app.agent.tools.base import Tool, ToolResult
from backend.app.models import Contractor

logger = logging.getLogger(__name__)


def _parse_rate(value: str) -> float | None:
    """Extract a numeric rate from natural-language rate descriptions.

    Handles formats like "$85/hr", "$85/hour", "$85 per hour", "$85 an hour",
    "85 dollars", "$85.50", "$50-75/hr" (extracts first number), "$4500 per project",
    "Usually around $80", etc.

    Returns None for non-numeric values like "not sure" or "varies".
    """
    cleaned = str(value).replace(",", "").strip()

    # Try to find a dollar amount or plain number
    # Handles: $85, $85/hr, $85.50, 85, 85.00, etc.
    match = re.search(r"\$?\s*(\d+(?:\.\d+)?)", cleaned)
    if match:
        return float(match.group(1))

    return None


def create_profile_tools(db: Session, contractor: Contractor) -> list[Tool]:
    """Create profile update tools for the agent."""

    async def update_profile(
        name: str | None = None,
        trade: str | None = None,
        location: str | None = None,
        hourly_rate: str | float | None = None,
        business_hours: str | None = None,
        communication_style: str | None = None,
        soul_text: str | None = None,
    ) -> ToolResult:
        """Update the contractor's profile information."""
        updates: dict[str, str | float] = {}
        fields_updated: list[str] = []

        if name is not None:
            updates["name"] = str(name)
            fields_updated.append("name")

        if trade is not None:
            updates["trade"] = str(trade)
            fields_updated.append("trade")

        if location is not None:
            updates["location"] = str(location)
            fields_updated.append("location")

        if hourly_rate is not None:
            parsed = _parse_rate(str(hourly_rate))
            if parsed is not None:
                updates["hourly_rate"] = parsed
                fields_updated.append("hourly_rate")
            else:
                logger.warning("Could not parse hourly rate from value: %r", hourly_rate)
                return ToolResult(
                    content=f"Could not parse hourly rate from: {hourly_rate}",
                    is_error=True,
                )

        if business_hours is not None:
            updates["business_hours"] = str(business_hours)
            fields_updated.append("business_hours")

        if communication_style is not None:
            updates["preferences_json"] = json.dumps(
                {"communication_style": str(communication_style)}
            )
            fields_updated.append("communication_style")

        if soul_text is not None:
            updates["soul_text"] = str(soul_text)
            fields_updated.append("soul_text")

        if not updates:
            return ToolResult(
                content="No fields provided to update.",
                is_error=True,
            )

        # Apply updates directly to the contractor record
        allowed_fields = {
            "name",
            "trade",
            "location",
            "hourly_rate",
            "business_hours",
            "preferences_json",
            "soul_text",
        }
        for field, value in updates.items():
            if field in allowed_fields:
                setattr(contractor, field, value)

        db.commit()
        db.refresh(contractor)

        summary = ", ".join(fields_updated)
        return ToolResult(content=f"Profile updated: {summary}")

    return [
        Tool(
            name="update_profile",
            description=(
                "Update the contractor's profile information. "
                "Use when you learn their name, trade, location, rate, "
                "business hours, communication style, or bio. "
                "Only include fields you want to change."
            ),
            function=update_profile,
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Contractor's full name",
                    },
                    "trade": {
                        "type": "string",
                        "description": "Trade or profession (e.g. plumber, electrician)",
                    },
                    "location": {
                        "type": "string",
                        "description": "City or region where they work",
                    },
                    "hourly_rate": {
                        "type": "string",
                        "description": "Hourly rate (e.g. '$85/hr', '85')",
                    },
                    "business_hours": {
                        "type": "string",
                        "description": "Working hours (e.g. 'Mon-Fri 7am-5pm')",
                    },
                    "communication_style": {
                        "type": "string",
                        "description": "Preferred communication style (e.g. 'casual', 'formal')",
                    },
                    "soul_text": {
                        "type": "string",
                        "description": "Bio or personality description for the assistant",
                    },
                },
            },
        ),
    ]


def extract_profile_updates_from_tool_calls(
    tool_calls: list[dict[str, object]],
) -> dict[str, object]:
    """Extract profile updates from update_profile tool call records.

    Looks at tool call records produced by the agent loop and returns
    a dict of profile field names to values for any successful
    update_profile calls. Used by the router to check whether onboarding
    fields have been filled.
    """
    updates: dict[str, object] = {}

    for tc in tool_calls:
        if tc.get("name") != "update_profile":
            continue
        if tc.get("is_error"):
            continue
        args = tc.get("args", {})
        if not isinstance(args, dict):
            continue

        for field in ("name", "trade", "location", "business_hours", "soul_text"):
            val = args.get(field)
            if val is not None:
                updates[field] = str(val)

        rate = args.get("hourly_rate")
        if rate is not None:
            parsed = _parse_rate(str(rate))
            if parsed is not None:
                updates["hourly_rate"] = parsed

        style = args.get("communication_style")
        if style is not None:
            updates["preferences_json"] = json.dumps({"communication_style": str(style)})

    return updates
