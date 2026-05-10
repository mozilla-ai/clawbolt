"""Tenant messaging tool for AppFolio Vendor Portal.

AppFolio brokers SMS between vendor and tenant via an anonymized proxy
number minted per work order. The flow is two API calls: ``get_proxy_number``
to obtain (or refresh) the proxy, then a POST to
``tenant_vendor_conversations`` with the message and that number. We
hide that two-step behind a single tool.
"""

from __future__ import annotations

import logging
from typing import Any

from backend.app.agent.approval import ApprovalPolicy, PermissionLevel
from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolReceipt, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.integrations.appfolio_vendor.errors import (
    log_unexpected_response_shape,
    service_error_to_tool_result,
)
from backend.app.integrations.appfolio_vendor.params import AppFolioMessageTenantParams
from backend.app.integrations.appfolio_vendor.service import AppFolioVendorService

logger = logging.getLogger(__name__)


def _extract_proxy_number(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    body: dict[str, Any] = payload
    for key in ("phone_number", "proxy_number", "phoneNumber", "proxyNumber"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def build_conversation_tools(service: AppFolioVendorService) -> list[Tool]:
    """Return the AppFolio tenant-messaging tool."""

    async def appfolio_message_tenant(work_order_id: str, message: str) -> ToolResult:
        text = message.strip()
        if not text:
            return ToolResult(
                content="Message cannot be empty.",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )

        try:
            proxy = await service.get_proxy_number(work_order_id)
        except Exception as exc:
            return service_error_to_tool_result("obtaining tenant proxy number", exc)

        phone_number = _extract_proxy_number(proxy)
        if not phone_number:
            # The "tenant opted out" path is a legitimate empty
            # response, but it shares its shape with "AppFolio renamed
            # the field again." Surface the actual response so we can
            # adjust ``_extract_proxy_number`` if a new key shows up.
            log_unexpected_response_shape(
                f"appfolio_message_tenant proxy lookup (work_order_id={work_order_id})",
                proxy,
                expected=(
                    "dict with one of phone_number / proxy_number / "
                    "phoneNumber / proxyNumber set to a non-empty string"
                ),
            )
            return ToolResult(
                content=(
                    "AppFolio did not return a tenant proxy number for this"
                    " work order. The tenant may have opted out of SMS or the"
                    " work order may not have an associated tenant."
                ),
                is_error=True,
                error_kind=ToolErrorKind.NOT_FOUND,
            )

        try:
            await service.message_tenant(
                work_order_id=work_order_id,
                phone_number=phone_number,
                message=text,
            )
        except Exception as exc:
            return service_error_to_tool_result("sending tenant message", exc)

        return ToolResult(
            content=(f"Sent message to tenant on work order {work_order_id} via AppFolio proxy."),
            receipt=ToolReceipt(
                action="Sent AppFolio tenant SMS",
                target=f"#{work_order_id}",
            ),
        )

    return [
        Tool(
            name=ToolName.APPFOLIO_MESSAGE_TENANT,
            description=(
                "Send an SMS to the tenant on an AppFolio work order via"
                " AppFolio's anonymized proxy number."
            ),
            function=appfolio_message_tenant,
            params_model=AppFolioMessageTenantParams,
            usage_hint=(
                "Use for tenant communication tied to a work order. The vendor's"
                " real number is never exposed."
            ),
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                description_builder=lambda args: (
                    f"Send tenant SMS on AppFolio work order #{args.get('work_order_id', '?')}"
                ),
            ),
        ),
    ]
