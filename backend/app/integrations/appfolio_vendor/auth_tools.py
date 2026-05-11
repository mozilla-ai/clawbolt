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
                    "Ask the user to paste only the magic-link token"
                    " (the long string after 'magic_link_token=' in the"
                    " AppFolio email URL), not the full URL. iMessage"
                    " and other SMS clients strip query params from"
                    " pasted links, so the full URL would arrive without"
                    " the token."
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

        return ToolResult(
            content="AppFolio connected. Tools are now available.",
            receipt=ToolReceipt(
                action="Connected AppFolio Vendor Portal",
                target="vendor account",
            ),
        )

    return [
        Tool(
            name=ToolName.APPFOLIO_CONNECT,
            description=(
                "Connect AppFolio Vendor Portal with the magic-link token"
                " from the user's AppFolio email."
            ),
            function=appfolio_connect,
            params_model=AppFolioConnectParams,
            usage_hint=(
                "Use when the user wants to start using AppFolio tools."
                " Tell them: open vendor.appfolio.com, request a magic link,"
                " then paste only the token from the URL (everything after"
                " 'magic_link_token='), not the full URL. iMessage and"
                " other SMS clients strip query params from pasted links,"
                " so the full URL would arrive without the token."
            ),
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ALWAYS,
                description_builder=lambda args: "Connect AppFolio Vendor Portal",
            ),
        ),
    ]
