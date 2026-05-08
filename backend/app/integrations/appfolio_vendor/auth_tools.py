"""AppFolio Vendor Portal authentication tools.

``appfolio_connect`` is the only tool here: it accepts a magic-link URL,
exchanges it for a Bearer JWT via the OAuth2 token endpoint, and persists
the credential. It lives separately from work-order / payment tools
because it runs *before* a credential exists.
"""

from __future__ import annotations

import logging

from backend.app.agent.approval import ApprovalPolicy, PermissionLevel
from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolReceipt, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.integrations.appfolio_vendor.auth import (
    MagicLinkError,
    extract_magic_link_token,
    save_credential,
    upsert_fingerprint,
)
from backend.app.integrations.appfolio_vendor.params import AppFolioConnectParams
from backend.app.integrations.appfolio_vendor.service import (
    AppFolioError,
    exchange_magic_link,
)

logger = logging.getLogger(__name__)


def build_auth_tools(user_id: str) -> list[Tool]:
    """Return the AppFolio connect tool bound to one user."""

    async def appfolio_connect(magic_link: str) -> ToolResult:
        """Exchange a pasted magic link for a Bearer JWT and persist it."""
        try:
            token = extract_magic_link_token(magic_link)
        except MagicLinkError as exc:
            return ToolResult(
                content=f"Could not read magic link: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
                hint=(
                    "Paste the full URL from the AppFolio email,"
                    " including everything after '?magic_link_token='."
                ),
            )

        fingerprint = await upsert_fingerprint(user_id)
        try:
            result = await exchange_magic_link(magic_link_token=token)
        except AppFolioError as exc:
            logger.warning("AppFolio OAuth exchange failed: %s", exc)
            return ToolResult(
                content=f"AppFolio rejected the magic link: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.AUTH,
                hint=(
                    "Magic links can only be used once and expire quickly."
                    " Request a fresh link from vendor.appfolio.com and try again."
                ),
            )

        await save_credential(
            user_id=user_id,
            jwt=result.jwt,
            fingerprint=fingerprint,
            customer_ids=result.customer_ids,
            refresh_token=result.refresh_token,
        )

        count = len(result.customer_ids)
        customers = f" across {count} customer account(s)" if count else ""
        return ToolResult(
            content=f"AppFolio connected{customers}. Tools are now available.",
            receipt=ToolReceipt(
                action="Connected AppFolio Vendor Portal",
                target=f"{len(result.customer_ids)} customer(s)",
            ),
        )

    return [
        Tool(
            name=ToolName.APPFOLIO_CONNECT,
            description=(
                "Connect AppFolio Vendor Portal by pasting the magic-link URL"
                " from the user's AppFolio email."
            ),
            function=appfolio_connect,
            params_model=AppFolioConnectParams,
            usage_hint=(
                "Use when the user wants to start using AppFolio tools."
                " Tell them: open vendor.appfolio.com, request a magic link,"
                " then paste the full URL from the email."
            ),
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ALWAYS,
                description_builder=lambda args: "Connect AppFolio Vendor Portal",
            ),
        ),
    ]
