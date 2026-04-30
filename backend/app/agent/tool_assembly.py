"""Preview-only tool-list builder for system-prompt reconstruction.

The agent's runtime path in :mod:`backend.app.agent.router` builds its
tool list inline because it needs a fully-populated :class:`ToolContext`
(storage backend, outbound-publish hook, downloaded media) and a shared
mutable ``activated_specialists`` set that the ``list_capabilities``
closure and the agent loop can both observe. This module mirrors only
the *start-of-turn* shape of that list with stubbed context fields, so
preview consumers (the system-prompt endpoint, debugging surfaces) can
render the same tool guidelines the LLM sees on a fresh turn without
the runtime plumbing.

Keeping it intentionally separate avoids two coupling pitfalls:

* the runtime's shared mutable activation set leaking into preview code
  paths where it would be confusing or unsafe;
* preview callers being forced to construct fake storage/publish hooks
  just to read tool schemas.
"""

from __future__ import annotations

from backend.app.agent.stores import ToolConfigStore
from backend.app.agent.tools.base import Tool
from backend.app.agent.tools.registry import (
    ToolContext,
    create_list_capabilities_tool,
    default_registry,
    ensure_tool_modules_imported,
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
    disabled_sub_tools = await tool_config_store.get_disabled_sub_tool_names()

    tools = await default_registry.create_core_tools(
        tool_context,
        excluded_factories=disabled_groups or None,
        excluded_tool_names=disabled_sub_tools or None,
    )
    # Mirror router.py: specialist tools for connected integrations are
    # pre-activated at agent boot, so the preview's tool list reflects what
    # the LLM actually sees on a fresh turn.
    (
        ready_specialist_tools,
        ready_specialist_names,
    ) = await default_registry.create_ready_specialist_tools(
        tool_context,
        excluded_factories=disabled_groups or None,
        excluded_tool_names=disabled_sub_tools or None,
    )
    tools.extend(ready_specialist_tools)
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
                activated_specialists=ready_specialist_names,
            )
        )
    return tools
