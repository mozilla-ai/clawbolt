"""Endpoints for user tool configuration.

Users can view and toggle domain-specific agent tools. Core tools
(workspace, profile, memory, messaging) are always enabled.
"""

from typing import NamedTuple

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
    "estimate": _FactoryMeta(
        "Generate professional estimates and quotes with PDF output",
        domain_group="Local Management",
        domain_group_order=1,
    ),
    "invoice": _FactoryMeta(
        "Generate invoices with payment tracking and PDF output",
        domain_group="Local Management",
        domain_group_order=1,
    ),
    "email": _FactoryMeta(
        "Send estimates and invoices to clients via email",
        domain_group="Local Management",
        domain_group_order=1,
    ),
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


def _get_auto_disabled_groups(user_id: str) -> dict[str, str]:
    """Return a mapping of {factory_name: reason} for groups that should be auto-disabled.

    When QuickBooks is connected with a valid token, local estimate, invoice,
    and email tools are auto-disabled because QB handles those operations.
    If the token is expired or invalid, local tools remain available so users
    are never locked out of all document tools.
    """
    from backend.app.agent.tools.quickbooks_tools import get_qb_auto_disabled_groups

    return get_qb_auto_disabled_groups(user_id)


def _build_tool_list(
    disabled_names: set[str],
    auto_disabled: dict[str, str] | None = None,
) -> list[ToolConfigEntry]:
    """Build the full tool config list from the registry.

    Each registered factory becomes one entry. Factories in
    ``_CORE_FACTORIES`` are always enabled; others respect the
    user's disabled set and auto-disable rules.
    """
    auto_disabled = auto_disabled or {}
    entries: list[ToolConfigEntry] = []
    for name in sorted(default_registry.factory_names):
        is_core = name in _CORE_FACTORIES
        meta = _FACTORY_META.get(name)

        # Determine enabled state and auto-disable reason
        auto_reason = auto_disabled.get(name) if not is_core else None
        if is_core:
            enabled = True
        elif auto_reason:
            enabled = False
        else:
            enabled = name not in disabled_names

        entries.append(
            ToolConfigEntry(
                name=name,
                description=meta.description if meta else "",
                category="core" if is_core else "domain",
                domain_group=meta.domain_group if meta else "",
                domain_group_order=meta.domain_group_order if meta else 0,
                enabled=enabled,
                auto_disabled_reason=auto_reason,
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
    auto_disabled = _get_auto_disabled_groups(current_user.id)
    entries = _build_tool_list(disabled_names, auto_disabled=auto_disabled)
    return ToolConfigResponse(
        tools=[
            ToolConfigEntryResponse(
                name=e.name,
                description=e.description,
                category=e.category,
                domain_group=e.domain_group,
                domain_group_order=e.domain_group_order,
                enabled=e.enabled,
                auto_disabled_reason=e.auto_disabled_reason,
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

    # Check for attempts to enable auto-disabled tools
    auto_disabled = _get_auto_disabled_groups(current_user.id)
    for name, enabled in requested.items():
        if enabled and name in auto_disabled:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot enable '{name}': {auto_disabled[name]}",
            )

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
    entries = _build_tool_list(disabled_names, auto_disabled=auto_disabled)
    await store.save(entries)

    return ToolConfigResponse(
        tools=[
            ToolConfigEntryResponse(
                name=e.name,
                description=e.description,
                category=e.category,
                domain_group=e.domain_group,
                domain_group_order=e.domain_group_order,
                enabled=e.enabled,
                auto_disabled_reason=e.auto_disabled_reason,
            )
            for e in entries
        ]
    )
