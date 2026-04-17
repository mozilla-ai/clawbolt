"""Endpoints for user tool configuration.

Users can view and toggle domain-specific agent tools. Core tools
(workspace, profile, memory, messaging) are always enabled.
"""

from typing import NamedTuple

from fastapi import APIRouter, Depends, HTTPException

from backend.app.agent.approval import ApprovalStore, PermissionLevel, get_approval_store
from backend.app.agent.dto import SubToolEntry, ToolConfigEntry, UserData
from backend.app.agent.stores import ToolConfigStore
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
_CORE_FACTORIES: frozenset[str] = frozenset(
    {
        "calculator",
        "workspace",
        "profile",
        "memory",
        "messaging",
        "file",
        "media",
        "heartbeat",
        "integration",
    }
)

# Consolidated metadata for each factory group: description, display group,
# and sort order.  Adding a new tool only requires one entry here.


class _FactoryMeta(NamedTuple):
    description: str
    domain_group: str = ""
    domain_group_order: int = 0


_FACTORY_META: dict[str, _FactoryMeta] = {
    "calculator": _FactoryMeta("Evaluate mathematical expressions"),
    "workspace": _FactoryMeta("Read, write, and edit markdown files in the workspace"),
    "profile": _FactoryMeta("View and update user profile information"),
    "memory": _FactoryMeta("Save, recall, and forget long-term facts"),
    "messaging": _FactoryMeta("Send text and media replies to the user"),
    "file": _FactoryMeta("Upload and organize files in cloud storage"),
    "media": _FactoryMeta("Describe and discard staged photos (agent-native storage)"),
    "heartbeat": _FactoryMeta("View and edit heartbeat notes"),
    "integration": _FactoryMeta("Manage integrations, enable/disable tools, connect OAuth"),
    "quickbooks": _FactoryMeta(
        "Query, create, and manage QuickBooks Online entities",
        domain_group="Integrations",
        domain_group_order=2,
    ),
    "calendar": _FactoryMeta(
        "Read and manage Google Calendar events",
        domain_group="Integrations",
        domain_group_order=2,
    ),
    "companycam": _FactoryMeta(
        "Upload photos, search projects, and manage job documentation with CompanyCam",
        domain_group="Integrations",
        domain_group_order=2,
    ),
    "supplier_pricing": _FactoryMeta(
        "Search product prices at Home Depot",
        domain_group="Integrations",
        domain_group_order=3,
    ),
}


def _build_tool_list(
    disabled_names: set[str],
    disabled_sub_tools_map: dict[str, list[str]] | None = None,
    user_id: str | None = None,
) -> list[ToolConfigEntry]:
    """Build the full tool config list from the registry.

    Each registered factory becomes one entry. Factories in
    ``_CORE_FACTORIES`` are always enabled; others respect the
    user's disabled set.

    When *disabled_sub_tools_map* is provided, it maps factory names to
    lists of disabled individual tool names within that factory.

    When *user_id* is provided, per-user permission overrides from the
    ``ApprovalStore`` are resolved for each sub-tool.
    """
    sub_map = disabled_sub_tools_map or {}
    # Load permission data once to avoid repeated file reads per sub-tool.
    approval_store = get_approval_store() if user_id else None
    perm_data = (
        approval_store.load_user_permissions(user_id) if approval_store and user_id else None
    )
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
                permission_level=str(
                    ApprovalStore.resolve_permission(
                        perm_data,
                        st.name,
                        default=PermissionLevel(st.default_permission),
                    )
                )
                if perm_data is not None
                else st.default_permission,
                hidden_in_permissions=st.hidden_in_permissions,
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


def _get_auth_status(user: UserData | None = None) -> dict[str, str]:
    """Check auth_check for each specialist factory.

    Returns a mapping of factory_name -> reason for factories that are
    not configured or not authenticated. Empty dict means all configured.

    When *user* is provided, a stub ``User`` with the correct ``id`` is
    passed to auth_check so it can verify per-user tokens (OAuth, etc.).
    """
    from backend.app.agent.tools.registry import ToolContext
    from backend.app.models import User

    orm_user: User | None = None
    if user is not None:
        orm_user = User(id=user.id, user_id=user.user_id)
    ctx = ToolContext(user=orm_user)  # type: ignore[arg-type]
    status: dict[str, str] = {}
    for name in default_registry.specialist_factory_names:
        factory = default_registry._factories.get(name)
        if factory and factory.auth_check:
            try:
                reason = factory.auth_check(ctx)
            except AttributeError:
                reason = None
            if reason:
                status[name] = reason
    return status


def _entry_to_response(
    e: ToolConfigEntry,
    auth_issues: dict[str, str] | None = None,
) -> ToolConfigEntryResponse:
    """Convert a ToolConfigEntry DTO to an API response model."""
    issues = auth_issues or {}
    auth_reason = issues.get(e.name, "")
    return ToolConfigEntryResponse(
        name=e.name,
        description=e.description,
        category=e.category,
        domain_group=e.domain_group,
        domain_group_order=e.domain_group_order,
        enabled=e.enabled,
        configured=not bool(auth_reason),
        auth_message=auth_reason,
        sub_tools=[
            SubToolEntryResponse(
                name=st.name,
                description=st.description,
                enabled=st.enabled,
                permission_level=st.permission_level,
                hidden_in_permissions=st.hidden_in_permissions,
            )
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
    entries = _build_tool_list(disabled_names, disabled_sub_map, user_id=current_user.id)
    auth_issues = _get_auth_status(current_user)
    return ToolConfigResponse(tools=[_entry_to_response(e, auth_issues) for e in entries])


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
    entries = _build_tool_list(disabled_names, disabled_sub_map, user_id=current_user.id)
    await store.save(entries)

    auth_issues = _get_auth_status(current_user)
    return ToolConfigResponse(tools=[_entry_to_response(e, auth_issues) for e in entries])
