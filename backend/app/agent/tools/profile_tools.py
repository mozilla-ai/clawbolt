"""Profile introspection and update tools for the agent.

Provides view_profile for introspection and update_profile for modification
of core identity fields (name, assistant name).

Business details like trade, location, rates, hours, timezone, and communication
preferences are stored in USER.md via the generic workspace file tools instead.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from backend.app.agent.context import StoredToolInteraction
from backend.app.agent.file_store import UserData, get_user_store
from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolResult, ToolTags
from backend.app.agent.tools.names import ToolName

if TYPE_CHECKING:
    from backend.app.agent.tools.registry import ToolContext

logger = logging.getLogger(__name__)


class ViewProfileParams(BaseModel):
    """Parameters for the view_profile tool (no parameters needed)."""


class UpdateProfileParams(BaseModel):
    """Parameters for the update_profile tool."""

    name: str | None = Field(default=None, description="User's full name")
    assistant_name: str | None = Field(
        default=None,
        description="What the user calls their AI assistant",
    )


def _format_profile(user: UserData) -> str:
    """Format the user's profile as a human-readable summary.

    Returns a structured text block with core profile fields.
    Business details are in USER.md (use read_file to view).
    """
    lines: list[str] = []
    lines.append("User Profile:")
    lines.append(f"  Name: {user.name or 'Not set'}")

    assistant = user.assistant_name if user.assistant_name != "Clawbolt" else None
    lines.append(f"  AI Name: {assistant or 'Clawbolt (default)'}")
    lines.append(f"  Onboarding Complete: {'Yes' if user.onboarding_complete else 'No'}")
    lines.append("")
    lines.append("For more details (trade, location, rates, hours, preferences), read USER.md.")

    return "\n".join(lines)


def create_profile_tools(user: UserData) -> list[Tool]:
    """Create profile introspection and update tools for the agent."""

    async def view_profile() -> ToolResult:
        """View the user's current profile information."""
        store = get_user_store()
        refreshed = await store.get_by_id(user.id)
        return ToolResult(content=_format_profile(refreshed or user))

    async def update_profile(
        name: str | None = None,
        assistant_name: str | None = None,
    ) -> ToolResult:
        """Update the user's core profile fields."""
        updates: dict[str, str] = {}
        fields_updated: list[str] = []

        if name is not None:
            updates["name"] = str(name)
            fields_updated.append("name")

        if assistant_name is not None:
            updates["assistant_name"] = str(assistant_name)
            fields_updated.append("assistant_name")

        if not updates:
            return ToolResult(
                content="No fields provided to update.",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )

        # Defense-in-depth: only allow known profile fields to be updated.
        _allowed_fields = {"name", "assistant_name"}
        safe_updates = {k: v for k, v in updates.items() if k in _allowed_fields}

        store = get_user_store()
        await store.update(user.id, **safe_updates)

        summary = ", ".join(fields_updated)
        return ToolResult(content=f"Profile updated: {summary}")

    return [
        Tool(
            name=ToolName.VIEW_PROFILE,
            description=(
                "View the user's current profile information. "
                "Use when the user asks what you know about them, "
                "or when you need to check their current profile details."
            ),
            function=view_profile,
            params_model=ViewProfileParams,
            usage_hint=(
                "When asked 'what do you know about me?' or needing to check "
                "the user's profile, use this tool first. "
                "For full details, also read USER.md."
            ),
        ),
        Tool(
            name=ToolName.UPDATE_PROFILE,
            description=(
                "Update core profile fields: name or AI assistant name. "
                "For other details (trade, location, rates, hours, preferences, personality), "
                "use write_file or edit_file on USER.md or SOUL.md instead."
            ),
            function=update_profile,
            params_model=UpdateProfileParams,
            tags={ToolTags.MODIFIES_PROFILE},
            usage_hint=(
                "Use this for name and assistant name. "
                "Use write_file/edit_file for everything else (USER.md, SOUL.md)."
            ),
        ),
    ]


def _profile_factory(ctx: ToolContext) -> list[Tool]:
    """Factory for profile tools, used by the registry."""
    return create_profile_tools(ctx.user)


def _register() -> None:
    from backend.app.agent.tools.registry import default_registry

    default_registry.register("profile", _profile_factory)


_register()


def extract_profile_updates_from_tool_calls(
    tool_calls: list[StoredToolInteraction],
) -> dict[str, object]:
    """Extract profile updates from update_profile tool call records.

    Looks at tool call records produced by the agent loop and returns
    a dict of profile field names to values for any successful
    update_profile calls. Used by the router to check whether onboarding
    fields have been filled.
    """
    updates: dict[str, object] = {}

    for tc in tool_calls:
        if tc.name != ToolName.UPDATE_PROFILE:
            continue
        if tc.is_error:
            continue
        args = tc.args

        for field in ("name", "assistant_name"):
            val = args.get(field)
            if val is not None:
                updates[field] = str(val)

    return updates
