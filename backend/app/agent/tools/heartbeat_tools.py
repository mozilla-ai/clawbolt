"""Heartbeat management tools for the agent."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from backend.app.agent.approval import ApprovalPolicy, PermissionLevel
from backend.app.agent.file_store import HeartbeatStore
from backend.app.agent.tools.base import Tool, ToolResult
from backend.app.agent.tools.names import ToolName

if TYPE_CHECKING:
    from backend.app.agent.tools.registry import ToolContext


class GetHeartbeatParams(BaseModel):
    """Parameters for the get_heartbeat tool (no parameters)."""


class UpdateHeartbeatParams(BaseModel):
    """Parameters for the update_heartbeat tool."""

    text: str = Field(description="The full updated heartbeat markdown text")


def create_heartbeat_tools(user_id: str) -> list[Tool]:
    """Create heartbeat-related tools for the agent."""

    async def get_heartbeat() -> ToolResult:
        """Read the user's heartbeat notes."""
        store = HeartbeatStore(user_id)
        text = store.read_heartbeat_md()
        if not text:
            return ToolResult(content="No heartbeat notes set.")
        return ToolResult(content=text)

    async def update_heartbeat(text: str) -> ToolResult:
        """Update the user's heartbeat notes.

        Reads the current content first so the result shows what changed.
        """
        store = HeartbeatStore(user_id)
        previous = store.read_heartbeat_md()
        await store.write_heartbeat_md(text)
        if previous:
            return ToolResult(content=f"Heartbeat notes updated.\n\nPrevious content:\n{previous}")
        return ToolResult(content="Heartbeat notes updated (was empty).")

    return [
        Tool(
            name=ToolName.GET_HEARTBEAT,
            description="Read the user's heartbeat notes.",
            function=get_heartbeat,
            params_model=GetHeartbeatParams,
            usage_hint="When asked about heartbeat notes or reminders, read them.",
        ),
        Tool(
            name=ToolName.UPDATE_HEARTBEAT,
            description=(
                "Update the user's heartbeat notes with new markdown text. "
                "IMPORTANT: This overwrites the entire file. Only include items "
                "that currently exist in the file plus whatever the user asked to "
                "add or change. Never re-add items that are not in the current "
                "file, even if you remember them from conversation history."
            ),
            function=update_heartbeat,
            params_model=UpdateHeartbeatParams,
            usage_hint=(
                "Always call get_heartbeat first to see the current content. "
                "Only add, remove, or change what the user explicitly asked for. "
                "Do not restore items that were previously deleted."
            ),
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ALWAYS,
                description_builder=lambda args: "Update heartbeat notes",
            ),
        ),
    ]


def _heartbeat_factory(ctx: ToolContext) -> list[Tool]:
    """Factory for heartbeat tools, used by the registry."""
    return create_heartbeat_tools(ctx.user.id)


def _register() -> None:
    from backend.app.agent.tools.registry import SubToolInfo, default_registry

    default_registry.register(
        "heartbeat",
        _heartbeat_factory,
        core=True,
        summary="View and edit heartbeat notes",
        sub_tools=[
            SubToolInfo(ToolName.GET_HEARTBEAT, "Read heartbeat notes"),
            SubToolInfo(ToolName.UPDATE_HEARTBEAT, "Update heartbeat notes"),
        ],
    )


_register()
