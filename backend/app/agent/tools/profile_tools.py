"""Profile introspection and update tools for the agent.

Provides view_profile for introspection and update_profile for modification,
enabling the agent to answer "what do you know about me?" and to update
profile fields when the contractor corrects information.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, cast

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolResult
from backend.app.models import Contractor

if TYPE_CHECKING:
    from backend.app.agent.tools.registry import ToolContext

logger = logging.getLogger(__name__)


class ViewProfileParams(BaseModel):
    """Parameters for the view_profile tool (no parameters needed)."""


class UpdateProfileParams(BaseModel):
    """Parameters for the update_profile tool."""

    name: str | None = Field(default=None, description="Contractor's full name")
    trade: str | None = Field(
        default=None,
        description="Trade or profession (e.g. plumber, electrician)",
    )
    location: str | None = Field(
        default=None,
        description="City or region where they work",
    )
    hourly_rate: str | None = Field(
        default=None,
        description="Hourly rate (e.g. '$85/hr', '85')",
    )
    business_hours: str | None = Field(
        default=None,
        description="Working hours (e.g. 'Mon-Fri 7am-5pm')",
    )
    timezone: str | None = Field(
        default=None,
        description="IANA timezone (e.g. 'America/New_York', 'America/Los_Angeles')",
    )
    communication_style: str | None = Field(
        default=None,
        description="Preferred communication style (e.g. 'casual', 'formal')",
    )
    soul_text: str | None = Field(
        default=None,
        description="Bio or personality description for the assistant",
    )


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


def _format_profile(contractor: Contractor) -> str:
    """Format the contractor's profile as a human-readable summary.

    Returns a structured text block with all known profile fields.
    Fields that are empty or unset are listed as "Not set".
    """
    lines: list[str] = []
    lines.append("Contractor Profile:")
    lines.append(f"  Name: {contractor.name or 'Not set'}")
    lines.append(f"  Trade: {contractor.trade or 'Not set'}")
    lines.append(f"  Location: {contractor.location or 'Not set'}")

    if contractor.hourly_rate:
        lines.append(f"  Hourly Rate: ${contractor.hourly_rate:.0f}/hr")
    else:
        lines.append("  Hourly Rate: Not set")

    lines.append(f"  Business Hours: {contractor.business_hours or 'Not set'}")
    lines.append(f"  Timezone: {contractor.timezone or 'Not set'}")
    lines.append(f"  Phone: {contractor.phone or 'Not set'}")

    # Communication style from preferences_json
    communication_style = None
    if contractor.preferences_json and contractor.preferences_json != "{}":
        try:
            prefs = json.loads(contractor.preferences_json)
            if isinstance(prefs, dict):
                communication_style = prefs.get("communication_style")
        except (json.JSONDecodeError, TypeError):
            pass
    lines.append(f"  Communication Style: {communication_style or 'Not set'}")

    lines.append(f"  Soul/Bio: {contractor.soul_text or 'Not set'}")
    lines.append(f"  Onboarding Complete: {'Yes' if contractor.onboarding_complete else 'No'}")

    return "\n".join(lines)


def create_profile_tools(db: Session, contractor: Contractor) -> list[Tool]:
    """Create profile introspection and update tools for the agent."""

    async def view_profile() -> ToolResult:
        """View the contractor's current profile information."""
        db.refresh(contractor)
        return ToolResult(content=_format_profile(contractor))

    async def update_profile(
        name: str | None = None,
        trade: str | None = None,
        location: str | None = None,
        hourly_rate: str | float | None = None,
        business_hours: str | None = None,
        timezone: str | None = None,
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
                    error_kind=ToolErrorKind.VALIDATION,
                )

        if business_hours is not None:
            updates["business_hours"] = str(business_hours)
            fields_updated.append("business_hours")

        if timezone is not None:
            updates["timezone"] = str(timezone)
            fields_updated.append("timezone")

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
                error_kind=ToolErrorKind.VALIDATION,
            )

        # Apply updates directly to the contractor record
        allowed_fields = {
            "name",
            "trade",
            "location",
            "hourly_rate",
            "business_hours",
            "timezone",
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
            name="view_profile",
            description=(
                "View the contractor's current profile information. "
                "Use when the contractor asks what you know about them, "
                "or when you need to check their current profile details."
            ),
            function=view_profile,
            params_model=ViewProfileParams,
            usage_hint=(
                "When asked 'what do you know about me?' or needing to check "
                "the contractor's profile, use this tool first."
            ),
        ),
        Tool(
            name="update_profile",
            description=(
                "Update the contractor's profile information. "
                "Use when you learn their name, trade, location, rate, "
                "business hours, communication style, or bio. "
                "Only include fields you want to change."
            ),
            function=update_profile,
            params_model=UpdateProfileParams,
            usage_hint=("Use this to update known contractor details (name, trade, rates, etc.)."),

        ),
    ]


def _profile_factory(ctx: ToolContext) -> list[Tool]:
    """Factory for profile tools, used by the registry."""
    return create_profile_tools(ctx.db, ctx.contractor)


def _register() -> None:
    from backend.app.agent.tools.registry import default_registry

    default_registry.register("profile", _profile_factory)


_register()


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
        args_raw = tc.get("args", {})
        if not isinstance(args_raw, dict):
            continue
        args = cast(dict[str, object], args_raw)

        for field in ("name", "trade", "location", "business_hours", "timezone", "soul_text"):
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
