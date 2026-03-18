"""Endpoints for user tool configuration.

Users can view and toggle domain-specific agent tools. Core tools
(workspace, profile, memory, messaging) are always enabled.
"""

from typing import NamedTuple

from fastapi import APIRouter, Depends, HTTPException

from backend.app.agent.dto import SubToolEntry
from backend.app.agent.file_store import (
    ToolConfigEntry,
    ToolConfigStore,
    UserData,
)
from backend.app.agent.tools.registry import (
    default_registry,
    ensure_tool_modules_imported,
)
from backend.app.auth.dependencies import get_current_user
from backend.app.schemas import (
    SubToolEntryResponse,
    ToolConfigEntryResponse,
    ToolConfigResponse,
    ToolConfigUpdate,
)

router = APIRouter()

# Ensure tool modules are loaded so the registry has all factories.
ensure_tool_modules_imported()

# Factories whose tools are always available and cannot be disabled.
_CORE_FACTORIES: frozenset[str] = frozenset({"workspace", "profile", "memory", "messaging"})

# Consolidated metadata for each factory group: description, display group,
# and sort order.  Adding a new tool only requires one entry here.


class _FactoryMeta(NamedTuple):
    description: str
    domain_group: str = ""
    domain_group_order: int = 0


_FACTORY_META: dict[str, _FactoryMeta] = {
    "workspace": _FactoryMeta("Read, write, and edit markdown files in the workspace"),
    "profile": _FactoryMeta("View and update user profile information"),
    "memory": _FactoryMeta("Save, recall, and forget long-term facts"),
    "messaging": _FactoryMeta("Send text and media replies to the user"),
    "file": _FactoryMeta(
        "Upload and organize files in cloud storage",
        domain_group="Local Management",
        domain_group_order=1,
    ),
    "heartbeat": _FactoryMeta(
        "View and edit heartbeat notes",
        domain_group="Local Management",
        domain_group_order=1,
    ),
    "quickbooks": _FactoryMeta(
        "Query, create, and manage QuickBooks Online entities",
        domain_group="Integrations",
        domain_group_order=2,
    ),
}


def _build_tool_list(
    disabled_names: set[str],
    disabled_sub_tools_map: dict[str, list[str]] | None = None,
) -> list[ToolConfigEntry]:
    """Build the full tool config list from the registry.

    Each registered factory becomes one entry. Factories in
    ``_CORE_FACTORIES`` are always enabled; others respect the
    user's disabled set.

    When *disabled_sub_tools_map* is provided, it maps factory names to
    lists of disabled individual tool names within that factory.
    """
    sub_map = disabled_sub_tools_map or {}
    entries: list[ToolConfigEntry] = []
    for name in sorted(default_registry.factory_names):
        is_core = name in _CORE_FACTORIES
        meta = _FACTORY_META.get(name)

        enabled = True if is_core else name not in disabled_names

        # Build sub-tool entries from registry metadata
        factory_sub_tools = default_registry.get_factory_sub_tools(name)
        disabled_subs = set(sub_map.get(name, []))
        sub_tool_entries = [
            SubToolEntry(
                name=st.name,
                description=st.description,
                enabled=st.name not in disabled_subs,
            )
            for st in factory_sub_tools
        ]

        entries.append(
            ToolConfigEntry(
                name=name,
                description=meta.description if meta else "",
                category="core" if is_core else "domain",
                domain_group=meta.domain_group if meta else "",
                domain_group_order=meta.domain_group_order if meta else 0,
                enabled=enabled,
                sub_tools=sub_tool_entries,
                disabled_sub_tools=list(disabled_subs),
            )
        )
    return entries


def _entry_to_response(e: ToolConfigEntry) -> ToolConfigEntryResponse:
    """Convert a ToolConfigEntry DTO to an API response model."""
    return ToolConfigEntryResponse(
        name=e.name,
        description=e.description,
        category=e.category,
        domain_group=e.domain_group,
        domain_group_order=e.domain_group_order,
        enabled=e.enabled,
        sub_tools=[
            SubToolEntryResponse(name=st.name, description=st.description, enabled=st.enabled)
            for st in e.sub_tools
        ],
    )


@router.get("/user/tools", response_model=ToolConfigResponse)
async def get_tool_config(
    current_user: UserData = Depends(get_current_user),
) -> ToolConfigResponse:
    """Return the current tool configuration for the user."""
    store = ToolConfigStore(current_user.id)
    saved = await store.load()
    disabled_names = {e.name for e in saved if not e.enabled}
    disabled_sub_map = {e.name: e.disabled_sub_tools for e in saved if e.disabled_sub_tools}
    entries = _build_tool_list(disabled_names, disabled_sub_map)
    return ToolConfigResponse(tools=[_entry_to_response(e) for e in entries])


@router.put("/user/tools", response_model=ToolConfigResponse)
async def update_tool_config(
    body: ToolConfigUpdate,
    current_user: UserData = Depends(get_current_user),
) -> ToolConfigResponse:
    """Update tool configuration for the user.

    Only domain-specific tools can be toggled. Attempts to disable
    core tools are silently ignored.

    Each entry may include ``disabled_sub_tools`` to control individual
    tools within a factory group.
    """
    if not body.tools:
        raise HTTPException(status_code=400, detail="No tools to update")

    # Load current config to merge with
    store = ToolConfigStore(current_user.id)
    saved = await store.load()
    disabled_names = {e.name for e in saved if not e.enabled}
    disabled_sub_map: dict[str, list[str]] = {
        e.name: e.disabled_sub_tools for e in saved if e.disabled_sub_tools
    }

    # Apply changes, ignoring core tools
    valid_factories = set(default_registry.factory_names)
    for update_entry in body.tools:
        name = update_entry.name
        if name not in valid_factories:
            continue
        if name in _CORE_FACTORIES:
            # Core tools cannot be disabled at factory level
            pass
        elif update_entry.enabled:
            disabled_names.discard(name)
        else:
            disabled_names.add(name)

        # Handle sub-tool toggles (applies to both core and domain factories,
        # allowing fine-grained control like read-only workspace mode).
        if update_entry.disabled_sub_tools is not None:
            # Validate sub-tool names against registry metadata
            valid_sub_names = {st.name for st in default_registry.get_factory_sub_tools(name)}
            filtered = [s for s in update_entry.disabled_sub_tools if s in valid_sub_names]
            if filtered:
                disabled_sub_map[name] = filtered
            else:
                disabled_sub_map.pop(name, None)

    # Build and save the full config
    entries = _build_tool_list(disabled_names, disabled_sub_map)
    await store.save(entries)

    return ToolConfigResponse(tools=[_entry_to_response(e) for e in entries])
