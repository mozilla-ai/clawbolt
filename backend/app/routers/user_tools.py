"""Endpoints for user tool configuration.

Users can view and toggle domain-specific agent tools. Core tools
(workspace, profile, memory, messaging) are always enabled.
"""

from fastapi import APIRouter, Depends, HTTPException

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
    ToolConfigEntryResponse,
    ToolConfigResponse,
    ToolConfigUpdate,
)

router = APIRouter()

# Ensure tool modules are loaded so the registry has all factories.
ensure_tool_modules_imported()

# Factories whose tools are always available and cannot be disabled.
_CORE_FACTORIES: frozenset[str] = frozenset({"workspace", "profile", "memory", "messaging"})

# Human-readable descriptions for each factory group.
_FACTORY_DESCRIPTIONS: dict[str, str] = {
    "workspace": "Read, write, and edit markdown files in the workspace",
    "profile": "View and update user profile information",
    "memory": "Save, recall, and forget long-term facts",
    "messaging": "Send text and media replies to the user",
    "estimate": "Generate professional estimates and quotes with PDF output",
    "file": "Upload and organize files in cloud storage",
    "checklist": "Manage recurring reminders and task checklists",
}


def _build_tool_list(
    disabled_names: set[str],
) -> list[ToolConfigEntry]:
    """Build the full tool config list from the registry.

    Each registered factory becomes one entry. Factories in
    ``_CORE_FACTORIES`` are always enabled; others respect the
    user's disabled set.
    """
    entries: list[ToolConfigEntry] = []
    for name in sorted(default_registry.factory_names):
        is_core = name in _CORE_FACTORIES
        entries.append(
            ToolConfigEntry(
                name=name,
                description=_FACTORY_DESCRIPTIONS.get(name, ""),
                category="core" if is_core else "domain",
                enabled=True if is_core else name not in disabled_names,
            )
        )
    return entries


@router.get("/user/tools", response_model=ToolConfigResponse)
async def get_tool_config(
    current_user: UserData = Depends(get_current_user),
) -> ToolConfigResponse:
    """Return the current tool configuration for the user."""
    store = ToolConfigStore(current_user.id)
    saved = await store.load()
    disabled_names = {e.name for e in saved if not e.enabled}
    entries = _build_tool_list(disabled_names)
    return ToolConfigResponse(
        tools=[
            ToolConfigEntryResponse(
                name=e.name,
                description=e.description,
                category=e.category,
                enabled=e.enabled,
            )
            for e in entries
        ]
    )


@router.put("/user/tools", response_model=ToolConfigResponse)
async def update_tool_config(
    body: ToolConfigUpdate,
    current_user: UserData = Depends(get_current_user),
) -> ToolConfigResponse:
    """Update tool configuration for the user.

    Only domain-specific tools can be toggled. Attempts to disable
    core tools are silently ignored.
    """
    if not body.tools:
        raise HTTPException(status_code=400, detail="No tools to update")

    # Build a map of requested changes
    requested: dict[str, bool] = {t.name: t.enabled for t in body.tools}

    # Load current config to merge with
    store = ToolConfigStore(current_user.id)
    saved = await store.load()
    disabled_names = {e.name for e in saved if not e.enabled}

    # Apply changes, ignoring core tools
    valid_factories = set(default_registry.factory_names)
    for name, enabled in requested.items():
        if name not in valid_factories:
            continue
        if name in _CORE_FACTORIES:
            # Core tools cannot be disabled
            continue
        if enabled:
            disabled_names.discard(name)
        else:
            disabled_names.add(name)

    # Build and save the full config
    entries = _build_tool_list(disabled_names)
    await store.save(entries)

    return ToolConfigResponse(
        tools=[
            ToolConfigEntryResponse(
                name=e.name,
                description=e.description,
                category=e.category,
                enabled=e.enabled,
            )
            for e in entries
        ]
    )
