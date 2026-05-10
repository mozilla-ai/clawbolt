"""Profile read tool for AppFolio Vendor Portal.

The profile-update tool was dropped: its body shape was inferred from
a fragmentary SPA call site and never Playwright-verified, so we kept
it from accidentally sending malformed PATCHes against a vendor's real
account. Vendors update their profile in the AppFolio web UI.
"""

from __future__ import annotations

import logging
from typing import Any

from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.integrations.appfolio_vendor.errors import (
    log_unexpected_response_shape,
    service_error_to_tool_result,
)
from backend.app.integrations.appfolio_vendor.params import AppFolioGetProfileParams
from backend.app.integrations.appfolio_vendor.service import AppFolioVendorService

logger = logging.getLogger(__name__)


def _fmt_profile(payload: dict[str, Any]) -> str:
    """Render whichever subset of profile fields AppFolio returns."""
    user = payload.get("user") if isinstance(payload, dict) else None
    if isinstance(user, dict):
        block: dict[str, Any] = user
    elif isinstance(payload, dict):
        block = payload
    else:
        block = {}
    name_parts = [
        str(block.get("firstName") or block.get("first_name") or ""),
        str(block.get("lastName") or block.get("last_name") or ""),
    ]
    name = " ".join(part for part in name_parts if part).strip()
    company = (
        block.get("companyName")
        or block.get("company_name")
        or (block.get("company") or {}).get("name")
        or ""
    )
    email = block.get("email") or ""
    phone = block.get("phoneNumber") or block.get("phone_number") or ""
    lines = ["AppFolio profile:"]
    if name:
        lines.append(f"  Name: {name}")
    if company:
        lines.append(f"  Company: {company}")
    if email:
        lines.append(f"  Email: {email}")
    if phone:
        lines.append(f"  Phone: {phone}")
    if len(lines) == 1:
        lines.append("  (AppFolio returned no profile fields)")
    return "\n".join(lines)


def build_profile_tools(service: AppFolioVendorService) -> list[Tool]:
    """Return AppFolio profile tools."""

    async def appfolio_get_profile() -> ToolResult:
        try:
            payload = await service.get_profile()
        except Exception as exc:
            return service_error_to_tool_result("fetching profile", exc)

        if not isinstance(payload, dict) or not payload:
            return ToolResult(
                content="AppFolio returned no profile.",
                is_error=True,
                error_kind=ToolErrorKind.NOT_FOUND,
            )
        # If none of the recognized fields are populated (either at the
        # top level or under a ``user``/``company`` block), the response
        # shape has likely drifted; log so the next mismatch is
        # debuggable rather than just rendering an empty profile card.
        recognized_present = any(
            (payload.get(k) or (isinstance(payload.get("user"), dict) and payload["user"].get(k)))
            for k in (
                "firstName",
                "first_name",
                "lastName",
                "last_name",
                "companyName",
                "company_name",
                "email",
                "phoneNumber",
                "phone_number",
            )
        ) or isinstance(payload.get("company"), dict)
        if not recognized_present:
            log_unexpected_response_shape(
                "appfolio_get_profile",
                payload,
                expected=(
                    "dict with at least one of firstName/lastName/email/"
                    "phoneNumber/companyName at the top level or under a "
                    "'user' / 'company' sub-object"
                ),
            )
        return ToolResult(content=_fmt_profile(payload))

    return [
        Tool(
            name=ToolName.APPFOLIO_GET_PROFILE,
            description=(
                "Get the connected AppFolio vendor profile (name, company, email, phone)."
            ),
            function=appfolio_get_profile,
            params_model=AppFolioGetProfileParams,
            usage_hint=(
                "Use to confirm which AppFolio account is connected, or to"
                " answer 'who am I logged in as'."
            ),
        ),
    ]
