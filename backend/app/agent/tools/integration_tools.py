"""Integration management tool for chat-based control.

Gives the agent the ability to view integration status, enable/disable
tool groups, and connect/disconnect OAuth integrations, so users can
manage everything over chat without needing the web UI.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from backend.app.agent.stores import ToolConfigStore
from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.integrations.appfolio_vendor import auth as appfolio_auth
from backend.app.services.oauth import get_oauth_config, list_oauth_integrations, oauth_service

if TYPE_CHECKING:
    from backend.app.agent.tools.registry import ToolContext, ToolRegistry

logger = logging.getLogger(__name__)

# Tool groups that use magic-link / paste-token auth instead of OAuth. These
# do not show up in ``list_oauth_integrations()`` but should still be
# manageable through ``manage_integration`` so the agent has a single
# discovery surface for "connect <integration>".
_MAGIC_LINK_INTEGRATIONS: set[str] = {"appfolio_vendor"}

# Core factories that back a user-facing integration but should not surface
# in ``manage_integration`` listings or be enable/disable-able on their own.
# These are visibility-paired with another factory: when the user-facing
# integration is disabled, the backing factory follows. ``appfolio_auth``
# holds the magic-link connect tools that must stay on the schema even when
# ``appfolio_vendor`` reports "not connected"; ``servicetitan_auth`` plays
# the same role for the paste-credentials ``connect_servicetitan`` tool.
# From the user's perspective each pair is one integration.
_HIDDEN_CORE_FACTORIES: dict[str, str] = {
    "appfolio_auth": "appfolio_vendor",
    "servicetitan_auth": "servicetitan",
}


def _display_name_for_oauth(registry: ToolRegistry, oauth_name: str) -> str:
    """Look up the human-readable label for an OAuth integration via the registry.

    Falls back to the raw oauth name when no factory has registered a
    matching ``oauth_name`` (or the matching factory left ``display_name``
    blank), so a freshly added OAuth tuple entry still renders something
    readable while its factory is being wired up.
    """
    factory_name = registry.find_factory_by_oauth_name(oauth_name)
    if factory_name is None:
        return oauth_name
    return registry.get_display_name(factory_name)


def _build_available_integrations_hint(registry: ToolRegistry) -> str:
    """Return a sentence enumerating the integrations this deployment supports.

    Built from ``list_oauth_integrations()`` plus ``_MAGIC_LINK_INTEGRATIONS``
    so new integrations surface in the system prompt automatically once
    their factory is wired up. This is the LLM's authoritative signal that
    an integration exists: prior ``manage_integration`` results sitting in
    conversation history may reflect an older deployment.

    Lists every integration the code knows about, not just those whose
    admin credentials are wired up. The existing status flow surfaces the
    "not configured by admin" case cleanly, and the hint already instructs
    the agent to call action='status' before offering a connect link, so
    the model never claims a connectable capability that the status check
    would reject.
    """
    oauth_targets = sorted(list_oauth_integrations())
    magic_link_targets = sorted(_MAGIC_LINK_INTEGRATIONS)

    display_names = [_display_name_for_oauth(registry, name) for name in oauth_targets]
    display_names.extend(registry.get_display_name(name) for name in magic_link_targets)
    display_names.sort()

    all_targets = sorted({*oauth_targets, *magic_link_targets})
    target_tokens = ", ".join(f"'{name}'" for name in all_targets)

    return (
        f"Integrations this deployment supports: {', '.join(display_names)}. "
        f"Trust this list over any earlier manage_integration result in this "
        f"conversation; capabilities can change between deployments. "
        f"Valid connect targets: {target_tokens}."
    )


class ManageIntegrationParams(BaseModel):
    """Parameters for the manage_integration tool."""

    action: Literal["status", "enable", "disable", "connect", "disconnect"] = Field(
        description=(
            "Action to perform: "
            "'status' to list all integrations and their state, "
            "'enable' or 'disable' to toggle a tool group, "
            "'connect' to get an OAuth link for an integration, "
            "'disconnect' to remove an OAuth connection."
        ),
    )
    target: str | None = Field(
        default=None,
        description=(
            "Tool group name (for enable/disable) or OAuth integration name "
            "(for connect/disconnect). Not needed for status."
        ),
    )


def create_integration_tools(ctx: ToolContext) -> list[Tool]:
    """Create the manage_integration tool scoped to the current user."""
    from backend.app.agent.tools.registry import default_registry, ensure_tool_modules_imported

    ensure_tool_modules_imported()

    user_id = ctx.user.id
    available_integrations_hint = _build_available_integrations_hint(default_registry)

    async def manage_integration(
        action: str,
        target: str | None = None,
    ) -> ToolResult:
        if action == "status":
            return await _handle_status(user_id, default_registry)

        if target is None:
            return ToolResult(
                content=f"The '{action}' action requires a target. Specify a tool group name.",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )

        if action == "enable":
            return await _handle_enable(user_id, target, default_registry)
        if action == "disable":
            return await _handle_disable(user_id, target, default_registry)
        if action == "connect":
            return await _handle_connect(user_id, target, default_registry)
        if action == "disconnect":
            return await _handle_disconnect(user_id, target, default_registry)

        valid_actions = "status, enable, disable, connect, disconnect"
        return ToolResult(
            content=f"Unknown action '{action}'. Valid actions: {valid_actions}",
            is_error=True,
            error_kind=ToolErrorKind.VALIDATION,
        )

    return [
        Tool(
            name=ToolName.MANAGE_INTEGRATION,
            description=(
                "Manage integrations: view status, enable/disable tool groups, "
                "connect/disconnect OAuth integrations. "
                "Use this when the user asks about their integrations or wants to "
                "change what tools are available."
            ),
            function=manage_integration,
            params_model=ManageIntegrationParams,
            usage_hint=(
                f"Use manage_integration to help users control their integrations. "
                f"{available_integrations_hint} "
                f"Before offering ANY connect link, call action='status' first and "
                f"skip integrations already showing as connected (do not re-prompt "
                f"for something they already set up). "
                f"Call with action='connect' and a target from the list above to "
                f"generate an OAuth link the user can tap to connect. "
                f"For 'appfolio_vendor' you'll get magic-link instructions instead "
                f"of a URL; follow them and then call appfolio_connect directly. "
                f"Call with action='enable'/'disable' and target=group_name to toggle tools."
            ),
            # Enable/disable and connect/disconnect mutate the per-user
            # ``tool_configs`` row and the OAuth token store. Two of these
            # in the same turn must serialize to avoid lost updates.
            concurrency_group="user_integrations",
        ),
    ]


async def _handle_status(
    user_id: str,
    registry: ToolRegistry,
) -> ToolResult:
    """Build a status overview of all tool groups."""
    store = ToolConfigStore(user_id)
    disabled_groups = await store.get_disabled_tool_names()

    core_lines: list[str] = []
    integration_lines: list[str] = []

    for name in sorted(registry.factory_names):
        if name in _HIDDEN_CORE_FACTORIES:
            continue
        factory = registry.get_factory(name)
        if factory is None:
            continue

        display = registry.get_display_name(name)

        if factory.core:
            core_lines.append(f"- {name}: {display} (always enabled)")
        else:
            enabled = name not in disabled_groups
            status_parts: list[str] = ["enabled" if enabled else "disabled"]

            # Check OAuth connection status. Factories backed by OAuth
            # declare ``oauth_name`` at registration time; an empty value
            # means the factory is not OAuth-backed (e.g. magic-link or
            # purely local tools).
            oauth_name = factory.oauth_name
            if oauth_name:
                config = get_oauth_config(oauth_name)
                if config is not None and config.is_configured:
                    connected = await oauth_service.is_connected(user_id, oauth_name)
                    status_parts.append("connected" if connected else "not connected")
                else:
                    status_parts.append("not configured by admin")
            elif name in _MAGIC_LINK_INTEGRATIONS:
                connected = await _is_magic_link_connected(name, user_id)
                status_parts.append("connected" if connected else "not connected")

            integration_lines.append(f"- {name}: {display} ({', '.join(status_parts)})")

    lines: list[str] = []
    if core_lines:
        lines.append("Core tools:")
        lines.extend(core_lines)
    if integration_lines:
        if lines:
            lines.append("")
        lines.append("Integrations:")
        lines.extend(integration_lines)

    if not lines:
        return ToolResult(content="No tool groups registered.")

    return ToolResult(content="\n".join(lines))


def _hidden_factories_paired_with(target: str) -> list[str]:
    """Return any backing factory names that should follow ``target``'s
    enable/disable state. Hidden factories never appear directly in user-
    facing listings, so toggling them only happens via cascade from the
    user-facing factory they back.
    """
    return [hidden for hidden, paired in _HIDDEN_CORE_FACTORIES.items() if paired == target]


async def _handle_enable(
    user_id: str,
    target: str,
    registry: ToolRegistry,
) -> ToolResult:
    """Enable a tool group."""
    if target not in registry.factory_names or target in _HIDDEN_CORE_FACTORIES:
        available = [
            n
            for n in sorted(registry.factory_names)
            if n not in registry.core_factory_names and n not in _HIDDEN_CORE_FACTORIES
        ]
        return ToolResult(
            content=(
                f"Unknown tool group '{target}'. Available integrations: {', '.join(available)}"
            ),
            is_error=True,
            error_kind=ToolErrorKind.NOT_FOUND,
        )

    factory = registry.get_factory(target)
    if factory and factory.core:
        display = registry.get_display_name(target)
        return ToolResult(
            content=f"{display} is a core tool and is always enabled.",
        )

    store = ToolConfigStore(user_id)
    await store.set_enabled(target, enabled=True)
    for hidden in _hidden_factories_paired_with(target):
        await store.set_enabled(hidden, enabled=True)

    display = registry.get_display_name(target)
    logger.info("User %s enabled tool group '%s' via chat", user_id, target)
    return ToolResult(
        content=f"Enabled {display} tools. They will be available starting with your next message.",
    )


async def _handle_disable(
    user_id: str,
    target: str,
    registry: ToolRegistry,
) -> ToolResult:
    """Disable a tool group."""
    if target not in registry.factory_names or target in _HIDDEN_CORE_FACTORIES:
        available = [
            n
            for n in sorted(registry.factory_names)
            if n not in registry.core_factory_names and n not in _HIDDEN_CORE_FACTORIES
        ]
        return ToolResult(
            content=(
                f"Unknown tool group '{target}'. Available integrations: {', '.join(available)}"
            ),
            is_error=True,
            error_kind=ToolErrorKind.NOT_FOUND,
        )

    factory = registry.get_factory(target)
    if factory and factory.core:
        display = registry.get_display_name(target)
        return ToolResult(
            content=f"{display} is a core tool and cannot be disabled.",
            is_error=True,
            error_kind=ToolErrorKind.VALIDATION,
        )

    store = ToolConfigStore(user_id)
    await store.set_enabled(target, enabled=False)
    for hidden in _hidden_factories_paired_with(target):
        await store.set_enabled(hidden, enabled=False)

    display = registry.get_display_name(target)
    logger.info("User %s disabled tool group '%s' via chat", user_id, target)
    return ToolResult(
        content=f"Disabled {display} tools. This takes effect starting with your next message.",
    )


def _resolve_oauth_name(target: str, registry: ToolRegistry) -> str:
    """Resolve a connect/disconnect target to the underlying OAuth name.

    The agent may pass either a factory name (``calendar``) or the OAuth
    integration name itself (``google_calendar``). The first form goes
    through the factory's registered ``oauth_name``; the second falls
    through unchanged.
    """
    factory = registry.get_factory(target)
    if factory is not None and factory.oauth_name:
        return factory.oauth_name
    return target


async def _handle_connect(user_id: str, target: str, registry: ToolRegistry) -> ToolResult:
    """Generate an OAuth authorization URL for an integration."""
    # Hidden backing factories are never directly addressable by users; the
    # connect flow goes through the user-facing factory they back.
    if target in _HIDDEN_CORE_FACTORIES:
        return ToolResult(
            content=f"Unknown tool group '{target}'.",
            is_error=True,
            error_kind=ToolErrorKind.NOT_FOUND,
        )

    # Magic-link integrations have their own connect flow (paste-a-token)
    # and don't fit the OAuth URL model.
    if target in _MAGIC_LINK_INTEGRATIONS:
        return await _handle_magic_link_connect(user_id, target, registry)

    oauth_name = _resolve_oauth_name(target, registry)

    if oauth_name not in list_oauth_integrations():
        return ToolResult(
            content=(
                f"'{target}' does not use OAuth authentication. "
                f"OAuth integrations: {', '.join(list_oauth_integrations())}"
            ),
            is_error=True,
            error_kind=ToolErrorKind.VALIDATION,
        )

    display = _display_name_for_oauth(registry, oauth_name)

    config = get_oauth_config(oauth_name)
    if config is None or not config.is_configured:
        return ToolResult(
            content=(
                f"{display} is not configured. "
                "The admin needs to set up the integration credentials first."
            ),
            is_error=True,
            error_kind=ToolErrorKind.AUTH,
        )

    if await oauth_service.is_connected(user_id, oauth_name):
        return ToolResult(
            content=f"{display} is already connected. Use action='disconnect' first to reconnect.",
        )

    url = oauth_service.get_authorization_url(config, user_id, source="chat")
    logger.info("User %s requested OAuth connect link for '%s' via chat", user_id, oauth_name)
    return ToolResult(
        content=(
            f"To connect {display}, open this link:\n\n{url}\n\n"
            "After you approve access, the connection will be ready "
            "the next time you message me."
        ),
    )


async def _handle_disconnect(user_id: str, target: str, registry: ToolRegistry) -> ToolResult:
    """Remove OAuth tokens for an integration."""
    if target in _HIDDEN_CORE_FACTORIES:
        return ToolResult(
            content=f"Unknown tool group '{target}'.",
            is_error=True,
            error_kind=ToolErrorKind.NOT_FOUND,
        )

    if target in _MAGIC_LINK_INTEGRATIONS:
        return await _handle_magic_link_disconnect(user_id, target, registry)

    oauth_name = _resolve_oauth_name(target, registry)

    if oauth_name not in list_oauth_integrations():
        return ToolResult(
            content=(
                f"'{target}' does not use OAuth authentication. "
                f"OAuth integrations: {', '.join(list_oauth_integrations())}"
            ),
            is_error=True,
            error_kind=ToolErrorKind.VALIDATION,
        )

    display = _display_name_for_oauth(registry, oauth_name)

    if not await oauth_service.is_connected(user_id, oauth_name):
        return ToolResult(
            content=f"{display} is not currently connected.",
            is_error=True,
            error_kind=ToolErrorKind.NOT_FOUND,
        )

    await oauth_service.delete_token(user_id, oauth_name)
    logger.info("User %s disconnected OAuth for '%s' via chat", user_id, oauth_name)
    return ToolResult(
        content=(
            f"Disconnected {display}. "
            "The tools are still enabled but won't work until you reconnect."
        ),
    )


async def _is_magic_link_connected(target: str, user_id: str) -> bool:
    """Dispatch ``is_connected`` lookup for magic-link integrations."""
    if target == "appfolio_vendor":
        return await appfolio_auth.is_connected(user_id)
    return False


async def _handle_magic_link_connect(
    user_id: str, target: str, registry: ToolRegistry
) -> ToolResult:
    """Return paste-token instructions for a magic-link integration.

    These integrations cannot be connected with a single URL: the user
    has to request a magic link from the vendor's web UI and paste it
    back. We return that recipe so the agent can guide the user, then
    rely on the integration's own auth tool (e.g. ``appfolio_connect``)
    to finish the flow.
    """
    if target == "appfolio_vendor":
        display = registry.get_display_name(target)
        if await appfolio_auth.is_connected(user_id):
            return ToolResult(
                content=(
                    f"{display} is already connected. Use action='disconnect' first to reconnect."
                ),
            )
        logger.info(
            "User %s requested magic-link connect instructions for '%s' via chat",
            user_id,
            target,
        )
        return ToolResult(
            content=(
                f"{display} uses magic-link auth (no OAuth URL). "
                "Walk the user through these steps: "
                "(1) open vendor.appfolio.com on their phone or laptop, "
                "(2) enter their email and request a sign-in link, "
                "(3) when the email arrives, copy the full link and paste "
                "it back to you. Then call appfolio_connect with the "
                "pasted text."
            ),
        )
    return ToolResult(
        content=f"No magic-link connect flow registered for '{target}'.",
        is_error=True,
        error_kind=ToolErrorKind.VALIDATION,
    )


async def _handle_magic_link_disconnect(
    user_id: str, target: str, registry: ToolRegistry
) -> ToolResult:
    """Clear stored credentials for a magic-link integration."""
    if target == "appfolio_vendor":
        display = registry.get_display_name(target)
        if not await appfolio_auth.is_connected(user_id):
            return ToolResult(
                content=f"{display} is not currently connected.",
                is_error=True,
                error_kind=ToolErrorKind.NOT_FOUND,
            )
        await appfolio_auth.clear_credential(user_id)
        logger.info(
            "User %s disconnected magic-link integration '%s' via chat",
            user_id,
            target,
        )
        return ToolResult(
            content=(
                f"Disconnected {display}. "
                "The tools are still enabled but won't work until you reconnect."
            ),
        )
    return ToolResult(
        content=f"No magic-link disconnect flow registered for '{target}'.",
        is_error=True,
        error_kind=ToolErrorKind.VALIDATION,
    )


def _integration_factory(ctx: ToolContext) -> list[Tool]:
    """Factory for integration management tools, used by the registry."""
    return create_integration_tools(ctx)


def _register() -> None:
    from backend.app.agent.tools.registry import SubToolInfo, default_registry

    default_registry.register(
        "integration",
        _integration_factory,
        core=True,
        dashboard_description="Manage integrations, enable/disable tools, connect OAuth",
        dashboard_always_enabled=True,
        sub_tools=[
            SubToolInfo(
                ToolName.MANAGE_INTEGRATION,
                "View status, enable/disable tools, connect/disconnect integrations",
            ),
        ],
    )


_register()
