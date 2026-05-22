"""AppFolio Vendor Portal authentication tools.

``appfolio_connect`` is the only tool here. It no longer accepts a
magic-link URL in chat; instead it instructs the user to navigate to
the Clawbolt web app to enter their magic link securely. The tool
lives separately from the data tools because it must run *before* a
credential exists.

Credential input moved to the web UI per issue #1337: pasting secure
secrets in chat (iMessage, Telegram) leaves them permanently in chat
history. The web app provides a controlled form for magic-link entry.
"""

from __future__ import annotations

import logging

from backend.app.agent.approval import ApprovalPolicy, PermissionLevel
from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.integrations.appfolio_vendor.params import AppFolioConnectParams

logger = logging.getLogger(__name__)


def build_auth_tools(user_id: str) -> list[Tool]:
    """Return the AppFolio connect tool bound to one user."""

    async def appfolio_connect(magic_link: str) -> ToolResult:
        """
        Instruct the user to connect via the Clawbolt web app.

        Credentials are no longer accepted in chat to avoid leaking
        secure secrets into permanent chat history. The user must
        navigate to the Clawbolt web app to enter their magic link.
        """
        return ToolResult(
            content=(
                "AppFolio Vendor Portal connection now requires the Clawbolt"
                " web app. Please navigate to Settings > Tools > AppFolio"
                " Vendor Portal and click Connect to enter your magic link"
                " securely."
            ),
            is_error=True,
            error_kind=ToolErrorKind.AUTH,
            hint=(
                "Ask the user to open the Clawbolt web app, go to Settings"
                " > Tools > AppFolio Vendor Portal, and click Connect."
                " The web form will accept their magic link securely."
            ),
        )

    return [
        Tool(
            name=ToolName.APPFOLIO_CONNECT,
            description=(
                "Connect AppFolio Vendor Portal. The user must navigate to"
                " the Clawbolt web app (Settings > Tools > AppFolio Vendor"
                " Portal) to enter their magic link securely. Chat-based"
                " credential input is disabled for security reasons."
            ),
            function=appfolio_connect,
            params_model=AppFolioConnectParams,
            usage_hint=(
                "Use when the user wants to start using AppFolio tools."
                " Tell them: open the Clawbolt web app, go to Settings"
                " > Tools > AppFolio Vendor Portal, click Connect, and"
                " paste their magic link there. The link comes from"
                " vendor.appfolio.com - request a magic link and copy"
                " the token from the URL."
            ),
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ALWAYS,
                description_builder=lambda args: "Connect AppFolio Vendor Portal",
            ),
        ),
    ]
