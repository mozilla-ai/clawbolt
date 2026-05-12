"""Preview-only tool-list builder for system-prompt reconstruction.

The agent's runtime path in :mod:`backend.app.agent.router` builds its
tool list inline because it needs a fully-populated :class:`ToolContext`
(storage backend, outbound-publish hook, downloaded media). This module
mirrors that shape with stubbed context fields so preview consumers (the
system-prompt endpoint, debugging surfaces) can render the same tool
guidelines the LLM sees on a fresh turn without the runtime plumbing.

Kept separate so preview callers do not have to construct fake
storage/publish hooks just to read tool schemas.
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
    # Sub-tools the user marked ``"never"`` in PERMISSIONS.json are
    # filtered out of the LLM schema, mirroring the runtime router /
    # heartbeat flow so previews reflect the real tool list.
    disabled_sub_tools = await get_approval_store().get_never_tool_names(user.id)

    tools = await default_registry.create_core_tools(
        tool_context,
        excluded_factories=disabled_groups or None,
        excluded_tool_names=disabled_sub_tools or None,
    )
    # Mirror router.py: specialist tools for connected integrations are
    # loaded at agent boot, so the preview's tool list reflects what the
    # LLM actually sees on a fresh turn.
    ready_specialist_tools = await default_registry.create_ready_specialist_tools(
        tool_context,
        excluded_factories=disabled_groups or None,
        excluded_tool_names=disabled_sub_tools or None,
    )
    tools.extend(ready_specialist_tools)
    specialist_summaries = await default_registry.get_available_specialist_summaries(
        tool_context, excluded_factories=disabled_groups or None
    )
    unauthenticated = await default_registry.get_unauthenticated_specialists(
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
            )
        )
    return tools
