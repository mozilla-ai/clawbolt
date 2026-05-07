"""Profile read and update tools for AppFolio Vendor Portal."""

from __future__ import annotations

import logging
from typing import Any

from backend.app.agent.approval import ApprovalPolicy, PermissionLevel
from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolReceipt, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.integrations.appfolio_vendor.errors import service_error_to_tool_result
from backend.app.integrations.appfolio_vendor.params import (
    AppFolioGetProfileParams,
    AppFolioUpdateProfileParams,
)
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
        return ToolResult(content=_fmt_profile(payload))

    async def appfolio_update_profile(
        first_name: str = "",
        last_name: str = "",
        phone_number: str = "",
        company_name: str = "",
    ) -> ToolResult:
        if not (first_name or last_name or phone_number or company_name):
            return ToolResult(
                content="At least one profile field must be provided.",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )
        try:
            await service.update_profile(
                first_name=first_name or None,
                last_name=last_name or None,
                phone_number=phone_number or None,
                company_name=company_name or None,
            )
        except Exception as exc:
            return service_error_to_tool_result("updating profile", exc)
        changed = [
            f
            for f, v in [
                ("first_name", first_name),
                ("last_name", last_name),
                ("phone_number", phone_number),
                ("company_name", company_name),
            ]
            if v
        ]
        return ToolResult(
            content=f"Updated AppFolio profile: {', '.join(changed)}.",
            receipt=ToolReceipt(
                action="Updated AppFolio profile",
                target=", ".join(changed),
            ),
        )

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
        Tool(
            name=ToolName.APPFOLIO_UPDATE_PROFILE,
            description="Update fields on the AppFolio vendor profile: name, phone, company.",
            function=appfolio_update_profile,
            params_model=AppFolioUpdateProfileParams,
            usage_hint=(
                "Pass only the fields the user wants changed; empty strings"
                " leave that field as-is. Confirm changes with the user first."
            ),
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                description_builder=lambda args: (
                    "Update AppFolio profile: "
                    + ", ".join(
                        f"{k}={v!r}" for k, v in (args or {}).items() if isinstance(v, str) and v
                    )
                ),
            ),
        ),
    ]
