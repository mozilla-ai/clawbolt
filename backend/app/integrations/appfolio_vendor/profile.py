"""Profile read tool for AppFolio Vendor Portal."""

from __future__ import annotations

import logging
from typing import Any

from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.integrations.appfolio_vendor.params import AppFolioGetProfileParams
from backend.app.integrations.appfolio_vendor.service import (
    AppFolioError,
    AppFolioVendorService,
    AuthExpiredError,
)

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
        except AuthExpiredError:
            return ToolResult(
                content="AppFolio session expired while fetching profile.",
                is_error=True,
                error_kind=ToolErrorKind.AUTH,
                hint=("Tell the user to request a fresh magic link and re-run appfolio_connect."),
            )
        except AppFolioError as exc:
            return ToolResult(
                content=f"AppFolio profile lookup failed: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )
        except Exception as exc:
            logger.exception("AppFolio profile fetch unexpected failure")
            return ToolResult(
                content=f"Unexpected profile error: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.INTERNAL,
            )

        if not isinstance(payload, dict) or not payload:
            return ToolResult(
                content="AppFolio returned no profile.",
                is_error=True,
                error_kind=ToolErrorKind.NOT_FOUND,
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
