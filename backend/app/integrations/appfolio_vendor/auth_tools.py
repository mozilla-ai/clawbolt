"""AppFolio Vendor Portal authentication tools.

Two tools live here:

* ``appfolio_connect`` — first-time connect. Accepts a magic-link URL,
  exchanges it for a JWT against ``/access``, persists fingerprint +
  JWT, and surfaces whether 2FA is still required.
* ``appfolio_complete_2fa`` — submits a verification code against
  ``/two_factor_authentication/onboard``.

These tools live separately from work-order / payment tools because
they run *before* a credential exists, so the factory must register
them at module import time even when ``is_connected`` is False.
"""

from __future__ import annotations

import logging

from backend.app.agent.approval import ApprovalPolicy, PermissionLevel
from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolReceipt, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.config import settings
from backend.app.integrations.appfolio_vendor.auth import (
    MagicLinkError,
    extract_magic_link_token,
    load_credential,
    save_credential,
    upsert_fingerprint,
)
from backend.app.integrations.appfolio_vendor.params import (
    AppFolioCompleteTwoFactorParams,
    AppFolioConnectParams,
)
from backend.app.integrations.appfolio_vendor.service import (
    AppFolioError,
    exchange_magic_link,
    submit_two_factor,
)

logger = logging.getLogger(__name__)


def build_auth_tools(user_id: str) -> list[Tool]:
    """Return the AppFolio connect + 2FA tools bound to one user."""

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
            result = await exchange_magic_link(
                api_base=settings.appfolio_vendor_api_base,
                magic_link_token=token,
                fingerprint=fingerprint,
            )
        except AppFolioError as exc:
            logger.warning("AppFolio /access exchange failed: %s", exc)
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
        )

        if result.requires_two_factor:
            content = (
                "AppFolio connected, but a 2FA code is required to finish."
                " AppFolio just sent a verification code via SMS or email."
                " Pass it to appfolio_complete_2fa to finalize."
            )
        else:
            count = len(result.customer_ids)
            customers = f" across {count} customer account(s)" if count else ""
            content = f"AppFolio connected{customers}. Tools are now available."

        return ToolResult(
            content=content,
            receipt=ToolReceipt(
                action="Connected AppFolio Vendor Portal",
                target=f"{len(result.customer_ids)} customer(s)",
            ),
        )

    async def appfolio_complete_2fa(code: str) -> ToolResult:
        """Submit the 2FA code AppFolio sent during ``appfolio_connect``."""
        cred = await load_credential(user_id)
        if cred is None:
            return ToolResult(
                content=(
                    "No AppFolio session in progress. Start with appfolio_connect"
                    " by pasting a fresh magic-link URL."
                ),
                is_error=True,
                error_kind=ToolErrorKind.AUTH,
            )
        try:
            await submit_two_factor(
                api_base=settings.appfolio_vendor_api_base,
                jwt=cred.jwt,
                fingerprint=cred.fingerprint,
                code=code.strip(),
            )
        except AppFolioError as exc:
            return ToolResult(
                content=f"AppFolio rejected the 2FA code: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.AUTH,
                hint="Codes expire quickly. Re-run appfolio_connect to get a fresh code.",
            )
        return ToolResult(
            content="AppFolio 2FA verified. Tools are now available.",
            receipt=ToolReceipt(
                action="Verified AppFolio 2FA",
                target="Vendor Portal",
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
        Tool(
            name=ToolName.APPFOLIO_COMPLETE_2FA,
            description="Submit a 2FA code to finalize AppFolio connection.",
            function=appfolio_complete_2fa,
            params_model=AppFolioCompleteTwoFactorParams,
            usage_hint=(
                "Only use after appfolio_connect reports that 2FA is required."
                " Ask the user for the SMS or email code and pass it here."
            ),
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ALWAYS,
                description_builder=lambda args: "Verify AppFolio 2FA code",
            ),
        ),
    ]
