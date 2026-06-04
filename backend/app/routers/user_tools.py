"""Endpoints for user tool configuration.

Users can view and toggle domain-specific agent tools. Factories that
declare ``dashboard_always_enabled=True`` at registration cannot be
toggled off from the Settings UI.

Per-sub-tool preferences live in ``user_permissions`` (the same store
that backs ``PERMISSIONS.json``); the legacy ``disabled_sub_tools``
column on ``tool_configs`` was collapsed into ``permission_level``
values of ``"never"``.
"""

from fastapi import APIRouter, Depends, HTTPException

from backend.app.agent.approval import ApprovalStore, PermissionLevel, get_approval_store
from backend.app.agent.dto import SubToolEntry, ToolConfigEntry, UserData
from backend.app.agent.stores import ToolConfigStore
from backend.app.agent.tools.integration_tools import (
    _HIDDEN_CORE_FACTORIES,
    _WEB_CONNECT_INTEGRATIONS,
    _web_connect_available,
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
from backend.app.services.oauth import list_oauth_integrations

router = APIRouter()

# Ensure tool modules are loaded so the registry has all factories.
ensure_tool_modules_imported()


async def _build_tool_list(
    disabled_names: set[str],
    user_id: str | None = None,
) -> list[ToolConfigEntry]:
    """Build the full tool config list from the registry.

    Each registered factory becomes one entry. Factories that declare
    ``dashboard_always_enabled=True`` are always enabled; others respect
    the user's disabled set.

    When *user_id* is provided, per-user permission overrides from the
    ``ApprovalStore`` are resolved for each sub-tool so the response
    carries the user's preferred ``permission_level`` per sub-tool.
    """
    # Load permission data once to avoid repeated DB reads per sub-tool.
    approval_store = get_approval_store() if user_id else None
    perm_data = (
        await approval_store.load_user_permissions(user_id) if approval_store and user_id else None
    )
    entries: list[ToolConfigEntry] = []
    for name in sorted(default_registry.factory_names):
        # Hidden backing factories are part of a user-facing integration's
        # plumbing, not a separate dashboard row. ``_HIDDEN_CORE_FACTORIES``
        # is currently empty; the guard stays for future paired factories.
        if name in _HIDDEN_CORE_FACTORIES:
            continue
        factory = default_registry.get_factory(name)
        if factory is None:
            continue
        is_core = factory.dashboard_always_enabled
        enabled = True if is_core else name not in disabled_names

        # Build sub-tool entries from registry metadata. The permission
        # level resolves through the same logic the agent uses, so the
        # Settings UI surfaces exactly what the runtime will enforce.
        factory_sub_tools = default_registry.get_factory_sub_tools(name)
        sub_tool_entries = [
            SubToolEntry(
                name=st.name,
                description=st.description,
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
                description=factory.dashboard_description,
                category="core" if is_core else "domain",
                domain_group=factory.dashboard_group,
                domain_group_order=factory.dashboard_group_order,
                enabled=enabled,
                sub_tools=sub_tool_entries,
            )
        )
    return entries


async def _get_auth_status(user: UserData | None = None) -> dict[str, str]:
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
        factory = default_registry.get_factory(name)
        if factory and factory.auth_check:
            try:
                reason = await factory.auth_check(ctx)
            except AttributeError:
                reason = None
            if reason:
                status[name] = reason
    return status


def _effective_oauth_name(factory_name: str) -> str:
    """Return the OAuth integration backing *factory_name*, or empty.

    Resolves the factory's registered ``oauth_name`` when set, falling back
    to the factory name itself when that name is a registered OAuth
    integration. Lets the Settings UI render Connect/Disconnect for OAuth-
    backed tools without hand-maintaining a factory-to-OAuth map per
    integration in the frontend.

    Kept in user_tools.py rather than on ``ToolRegistry`` (the inverse
    direction, ``find_factory_by_oauth_name``, lives there) so the registry
    module stays free of an import from ``services/oauth.py``. The router is
    already the only consumer.
    """
    factory = default_registry.get_factory(factory_name)
    if factory is None:
        return ""
    candidate = factory.oauth_name or factory_name
    return candidate if candidate in list_oauth_integrations() else ""


def _entry_to_response(
    e: ToolConfigEntry,
    auth_issues: dict[str, str] | None = None,
) -> ToolConfigEntryResponse:
    """Convert a ToolConfigEntry DTO to an API response model."""
    issues = auth_issues or {}
    auth_reason = issues.get(e.name, "")
    factory = default_registry.get_factory(e.name)
    return ToolConfigEntryResponse(
        name=e.name,
        description=e.description,
        category=e.category,
        domain_group=e.domain_group,
        domain_group_order=e.domain_group_order,
        enabled=e.enabled,
        configured=not bool(auth_reason),
        auth_message=auth_reason,
        oauth_name=_effective_oauth_name(e.name),
        connect_form=(
            e.name if e.name in _WEB_CONNECT_INTEGRATIONS and _web_connect_available(e.name) else ""
        ),
        always_enabled=factory.dashboard_always_enabled if factory else False,
        sub_tools=[
            SubToolEntryResponse(
                name=st.name,
                description=st.description,
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
    entries = await _build_tool_list(disabled_names, user_id=current_user.id)
    auth_issues = await _get_auth_status(current_user)
    return ToolConfigResponse(tools=[_entry_to_response(e, auth_issues) for e in entries])


_VALID_PERMISSION_LEVELS = {level.value for level in PermissionLevel}


@router.put("/user/tools", response_model=ToolConfigResponse)
async def update_tool_config(
    body: ToolConfigUpdate,
    current_user: UserData = Depends(get_current_user),
) -> ToolConfigResponse:
    """Update tool configuration for the user.

    Only factories that are not ``dashboard_always_enabled`` can be
    toggled at the factory level. Attempts to disable always-enabled
    tools are silently ignored.

    Each entry may include a ``sub_tools`` list with explicit
    ``permission_level`` values to override individual sub-tools. Levels
    are persisted to ``user_permissions``; ``"never"`` filters the
    sub-tool out of the LLM schema on the next turn.
    """
    if not body.tools:
        raise HTTPException(status_code=400, detail="No tools to update")

    # Load current factory-level config to merge with.
    store = ToolConfigStore(current_user.id)
    saved = await store.load()
    disabled_names = {e.name for e in saved if not e.enabled}

    approval_store = get_approval_store()

    # Apply changes, ignoring always-enabled tools.
    valid_factories = set(default_registry.factory_names)
    for update_entry in body.tools:
        name = update_entry.name
        if name not in valid_factories:
            continue
        factory = default_registry.get_factory(name)
        if factory is not None and factory.dashboard_always_enabled:
            # Tools the dashboard renders as always-on cannot be disabled
            # via this endpoint; silently ignore the toggle.
            pass
        elif update_entry.enabled:
            disabled_names.discard(name)
        else:
            disabled_names.add(name)

        if update_entry.sub_tools:
            valid_sub_names = {st.name for st in default_registry.get_factory_sub_tools(name)}
            for sub_update in update_entry.sub_tools:
                if sub_update.name not in valid_sub_names:
                    continue
                if sub_update.permission_level not in _VALID_PERMISSION_LEVELS:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Invalid permission_level {sub_update.permission_level!r} "
                            f"for sub-tool {sub_update.name!r}. "
                            f"Allowed values: {', '.join(sorted(_VALID_PERMISSION_LEVELS))}"
                        ),
                    )
                await approval_store.set_permission(
                    current_user.id,
                    sub_update.name,
                    PermissionLevel(sub_update.permission_level),
                )

    # Build and save the full factory-level config.
    entries = await _build_tool_list(disabled_names, user_id=current_user.id)
    await store.save(entries)

    auth_issues = await _get_auth_status(current_user)
    return ToolConfigResponse(tools=[_entry_to_response(e, auth_issues) for e in entries])
