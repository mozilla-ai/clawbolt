"""ServiceTitan authentication tool.

``connect_servicetitan`` no longer accepts credentials pasted in chat.
Instead it instructs the user to navigate to the Clawbolt web app to
enter their Tenant ID, Client ID, and Client Secret securely. The tool
lives separately from the data-tools surface because it must remain
reachable before any credential exists, mirroring the AppFolio
``appfolio_connect`` split.

Credential input moved to the web UI per issue #1337: pasting secure
secrets in chat (iMessage, Telegram) leaves them permanently in chat
history. The web app provides a controlled form for credential entry.
"""

from __future__ import annotations

import logging

from backend.app.agent.approval import ApprovalPolicy, PermissionLevel
from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.integrations.servicetitan.params import ServiceTitanConnectParams

logger = logging.getLogger(__name__)


def build_auth_tools(user_id: str) -> list[Tool]:
    """Return the ServiceTitan connect tool bound to one user."""

    async def connect_servicetitan(
        tenant_id: str,
        client_id: str,
        client_secret: str,
    ) -> ToolResult:
        """
        Instruct the user to connect via the Clawbolt web app.

        Credentials are no longer accepted in chat to avoid leaking
        secure secrets into permanent chat history. The user must
        navigate to the Clawbolt web app to enter their Tenant ID,
        Client ID, and Client Secret.
        """
        return ToolResult(
            content=(
                "ServiceTitan connection now requires the Clawbolt web app."
                " Please navigate to Settings > Tools > ServiceTitan and"
                " click Connect to enter your Tenant ID, Client ID, and"
                " Client Secret securely."
            ),
            is_error=True,
            error_kind=ToolErrorKind.AUTH,
            hint=(
                "Ask the user to open the Clawbolt web app, go to Settings"
                " > Tools > ServiceTitan, and click Connect. The web form"
                " will accept their Tenant ID, Client ID, and Client Secret"
                " securely."
            ),
        )

    return [
        Tool(
            name=ToolName.SERVICETITAN_CONNECT,
            description=(
                "Connect a ServiceTitan tenant. The user must navigate to"
                " the Clawbolt web app (Settings > Tools > ServiceTitan)"
                " to enter their Tenant ID, Client ID, and Client Secret"
                " securely. Chat-based credential input is disabled for"
                " security reasons."
            ),
            function=connect_servicetitan,
            params_model=ServiceTitanConnectParams,
            usage_hint=(
                "Use when the user wants to start using ServiceTitan tools."
                " Tell them: open the Clawbolt web app, go to Settings"
                " > Tools > ServiceTitan, click Connect, and enter their"
                " Tenant ID, Client ID, and Client Secret. These come from"
                " ServiceTitan -> Settings -> Integrations -> API"
                " Application Access."
            ),
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ALWAYS,
                description_builder=lambda args: "Connect ServiceTitan",
            ),
            # Writes to ``oauth_tokens`` for this user; serialize with
            # other integration toggles so two concurrent connects (or a
            # connect racing a disconnect) cannot lose updates.
            concurrency_group="user_integrations",
        ),
    ]
