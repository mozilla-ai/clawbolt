"""ServiceTitan authentication tool.

``connect_servicetitan`` accepts the three values a user pastes during
onboarding (Tenant ID, Client ID, Client Secret), validates them by
minting a bearer token against the configured token endpoint, and
persists the credential. It lives separately from the (empty for now)
data-tools surface because it must remain reachable before any
credential exists, mirroring the AppFolio ``appfolio_connect`` split.
"""

from __future__ import annotations

import logging

from backend.app.agent.approval import ApprovalPolicy, PermissionLevel
from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolReceipt, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.config import settings
from backend.app.integrations.servicetitan.auth import (
    ServiceTitanAuthError,
    mint_access_token,
    save_credentials,
)
from backend.app.integrations.servicetitan.params import ServiceTitanConnectParams

logger = logging.getLogger(__name__)


def build_auth_tools(user_id: str) -> list[Tool]:
    """Return the ServiceTitan connect tool bound to one user."""

    async def connect_servicetitan(
        tenant_id: str,
        client_id: str,
        client_secret: str,
    ) -> ToolResult:
        """Validate the three pasted values, then persist them."""
        tenant_id = tenant_id.strip()
        client_id = client_id.strip()
        client_secret = client_secret.strip()
        if not tenant_id or not client_id or not client_secret:
            return ToolResult(
                content=("Tenant ID, Client ID, and Client Secret are all required."),
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
                hint=(
                    "Have the user paste all three values from ServiceTitan's"
                    " Settings -> Integrations -> API Application Access page."
                ),
            )
        if not settings.servicetitan_app_key:
            return ToolResult(
                content=(
                    "ServiceTitan is not configured: the deployment is missing"
                    " an App Key. Ask the operator to set SERVICETITAN_APP_KEY."
                ),
                is_error=True,
                error_kind=ToolErrorKind.AUTH,
            )

        try:
            access_token, expires_at = await mint_access_token(
                client_id=client_id,
                client_secret=client_secret,
            )
        except ServiceTitanAuthError as exc:
            logger.warning("ServiceTitan connect failed for user=%s: %s", user_id, exc)
            return ToolResult(
                content=f"ServiceTitan rejected the credentials: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.AUTH,
                hint=(
                    "Double-check the Tenant ID, Client ID, and Client Secret."
                    " Regenerate the secret in ServiceTitan if it was lost."
                ),
            )

        await save_credentials(
            user_id,
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
            app_key=settings.servicetitan_app_key,
            access_token=access_token,
            expires_at=expires_at,
        )

        return ToolResult(
            content="ServiceTitan connected. Tools are now available.",
            receipt=ToolReceipt(
                action="Connected ServiceTitan",
                target=f"tenant {tenant_id}",
            ),
        )

    return [
        Tool(
            name=ToolName.SERVICETITAN_CONNECT,
            description=(
                "Connect a ServiceTitan tenant by pasting the Tenant ID, Client"
                " ID, and Client Secret from the ServiceTitan API Application"
                " Access settings."
            ),
            function=connect_servicetitan,
            params_model=ServiceTitanConnectParams,
            usage_hint=(
                "Use when the user wants to start using ServiceTitan tools."
                " Walk them through: (1) open ServiceTitan -> Settings ->"
                " Integrations -> API Application Access, (2) create or"
                " open an application and copy the Client ID and Client"
                " Secret, (3) read the Tenant ID off the same page, then"
                " paste all three values back to you."
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
