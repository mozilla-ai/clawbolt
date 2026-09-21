"""The one place the agent's tool list is assembled.

Three callers need the same list: the runtime turn in
:mod:`backend.app.agent.router`, the heartbeat's Phase 2 agent, and the
system-prompt preview. They used to build it inline, three times, which
meant the preview could drift from what the agent was actually handed.

:func:`assemble_turn_tools` is that shared build.
:func:`build_initial_turn_tools` wraps it for preview callers, which have
no storage backend or outbound hook to supply.
"""

from __future__ import annotations

from backend.app.agent.approval import get_approval_store
from backend.app.agent.stores import ToolConfigStore
from backend.app.agent.tools.base import Tool
from backend.app.agent.tools.registry import (
    ToolContext,
    create_list_capabilities_tool,
    default_registry,
    ensure_tool_modules_imported,
)
from backend.app.models import User


async def assemble_turn_tools(
    tool_context: ToolContext,
    *,
    disabled_factories: set[str] | None = None,
    disabled_sub_tools: set[str] | None = None,
) -> tuple[list[Tool], dict[str, str]]:
    """Build the tool list the LLM sees for one turn.

    Core tools first, then specialist tools for every integration the user
    has connected (loaded from turn 1 so the model can call them without a
    discovery round trip), then ``list_capabilities`` when there is still
    something to discover.

    Returns the tools and the specialist summaries, which callers log.
    """
    tools = await default_registry.create_core_tools(
        tool_context,
        excluded_factories=disabled_factories or None,
        excluded_tool_names=disabled_sub_tools or None,
    )
    tools.extend(
        await default_registry.create_ready_specialist_tools(
            tool_context,
            excluded_factories=disabled_factories or None,
            excluded_tool_names=disabled_sub_tools or None,
        )
    )
    summaries = await default_registry.get_available_specialist_summaries(
        tool_context, excluded_factories=disabled_factories or None
    )
    unauthenticated = await default_registry.get_unauthenticated_specialists(
        tool_context, excluded_factories=disabled_factories or None
    )
    if summaries or unauthenticated:
        disabled_specialist_subs = default_registry.get_disabled_specialist_sub_tools(
            disabled_sub_tools or set()
        )
        tools.append(
            create_list_capabilities_tool(
                summaries,
                unauthenticated=unauthenticated,
                disabled_sub_tools=disabled_specialist_subs or None,
            )
        )
    return tools, summaries


async def build_initial_turn_tools(
    user: User,
    *,
    channel: str | None = None,
    to_address: str | None = None,
) -> list[Tool]:
    """Return the tools the agent would have at the start of a turn.

    This is core tools (always-on), specialist tools for integrations
    the user has authenticated for, plus the ``list_capabilities``
    meta-tool when there are unconnected integrations to surface for
    discovery.

    Storage, downloaded media, and outbound-publish hooks are left as
    stubs because callers of this helper only need the tools' schemas
    and usage hints (for system-prompt rendering or debugging) -- not
    their executors.
    """
    # The registry is auto-discovery-driven; ensure all *_tools modules
    # have run their _register() side effects before we ask it for the
    # current set of factories. Idempotent / cached after the first call.
    ensure_tool_modules_imported()

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
    # Sub-tools the user marked ``"never"`` in PERMISSIONS.json are filtered
    # out of the LLM schema, mirroring the runtime router / heartbeat flow so
    # previews reflect the real tool list.
    disabled_sub_tools = await get_approval_store().get_never_tool_names(user.id)

    tools, _summaries = await assemble_turn_tools(
        tool_context,
        disabled_factories=disabled_groups,
        disabled_sub_tools=disabled_sub_tools,
    )
    return tools
