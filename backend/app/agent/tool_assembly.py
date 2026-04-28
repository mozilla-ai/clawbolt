"""Build the tool list a fresh agent turn would start with.

The agent loop and the system-prompt preview endpoint both need to know
which tools the LLM sees at the start of a turn. Keeping that
construction in one place ensures the preview matches what the agent
actually receives.
"""

from __future__ import annotations

from backend.app.agent.stores import ToolConfigStore
from backend.app.agent.tools.base import Tool
from backend.app.agent.tools.registry import (
    ToolContext,
    create_list_capabilities_tool,
    default_registry,
)
from backend.app.models import User


async def build_initial_turn_tools(
    user: User,
    *,
    channel: str | None = None,
    to_address: str | None = None,
) -> list[Tool]:
    """Return the tools the agent would have at the start of a turn.

    This is core tools (always-on) plus the ``list_capabilities``
    meta-tool when there are specialist categories or unauthenticated
    services to surface. Specialist tools that get activated mid-turn
    via ``list_capabilities`` are NOT included here, matching how the
    agent itself starts each turn fresh.

    Storage, downloaded media, and outbound-publish hooks are left as
    stubs because callers of this helper only need the tools' schemas
    and usage hints (for system-prompt rendering or debugging) -- not
    their executors.
    """
    tool_context = ToolContext(
        user=user,
        storage=None,
        publish_outbound=None,
        channel=channel or "",
        to_address=to_address or "",
        downloaded_media=[],
        turn_text="",
    )

    tool_config_store = ToolConfigStore(user.id)
    disabled_groups = await tool_config_store.get_disabled_tool_names()
    disabled_sub_tools = await tool_config_store.get_disabled_sub_tool_names()

    tools = await default_registry.create_core_tools(
        tool_context,
        excluded_factories=disabled_groups or None,
        excluded_tool_names=disabled_sub_tools or None,
    )
    specialist_summaries = default_registry.get_available_specialist_summaries(
        tool_context, excluded_factories=disabled_groups or None
    )
    unauthenticated = default_registry.get_unauthenticated_specialists(
        tool_context, excluded_factories=disabled_groups or None
    )
    disabled_specialist_subs = default_registry.get_disabled_specialist_sub_tools(
        disabled_sub_tools or set()
    )
    if specialist_summaries or unauthenticated:
        tools.append(
            create_list_capabilities_tool(
                specialist_summaries,
                unauthenticated=unauthenticated,
                disabled_sub_tools=disabled_specialist_subs or None,
                activated_specialists=set(),
            )
        )
    return tools
