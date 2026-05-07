"""AppFolio Vendor Portal tool registration and factory.

Picked up by the registry's ``ensure_tool_modules_imported`` scan; the
``_register()`` call at the bottom installs the integration into the
default tool registry at module-import time.

Two registration concerns:

1. The auth tools (``appfolio_connect``, ``appfolio_complete_2fa``) need
   to be available *before* the user has a credential, so they are
   surfaced via ``auth_check`` returning a connect prompt rather than
   an empty tool list.
2. The data tools (work orders, payments, profile) require a loaded
   credential and so live in the factory body, hidden until connected.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from backend.app.agent.tools.base import Tool
from backend.app.agent.tools.names import ToolName
from backend.app.config import settings
from backend.app.integrations.appfolio_vendor.auth import (
    is_connected,
    load_credential,
)
from backend.app.integrations.appfolio_vendor.auth_tools import build_auth_tools
from backend.app.integrations.appfolio_vendor.compliance import build_compliance_tools
from backend.app.integrations.appfolio_vendor.conversations import build_conversation_tools
from backend.app.integrations.appfolio_vendor.estimates import build_estimate_tools
from backend.app.integrations.appfolio_vendor.invoices import build_invoice_tools
from backend.app.integrations.appfolio_vendor.notes import build_note_tools
from backend.app.integrations.appfolio_vendor.payments import build_payment_tools
from backend.app.integrations.appfolio_vendor.profile import build_profile_tools
from backend.app.integrations.appfolio_vendor.service import build_service
from backend.app.integrations.appfolio_vendor.work_order_writes import (
    build_work_order_write_tools,
)
from backend.app.integrations.appfolio_vendor.work_orders import build_work_order_tools

if TYPE_CHECKING:
    from backend.app.agent.tools.registry import ToolContext

logger = logging.getLogger(__name__)


_INTEGRATION = "appfolio_vendor"


async def _appfolio_factory(ctx: ToolContext) -> list[Tool]:
    """Assemble AppFolio tools for the given user.

    Always returns the two auth tools so the user can connect from a
    fresh state. Data tools are appended only when a usable credential
    is on file.
    """
    user_id = ctx.user.id
    tools: list[Tool] = list(build_auth_tools(user_id))
    cred = await load_credential(user_id)
    if cred is None or not cred.jwt:
        return tools
    service = build_service(cred, api_base=settings.appfolio_vendor_api_base)
    tools.extend(build_work_order_tools(service))
    tools.extend(build_work_order_write_tools(service))
    tools.extend(build_note_tools(service, ctx))
    tools.extend(build_conversation_tools(service))
    tools.extend(build_payment_tools(service))
    tools.extend(build_profile_tools(service))
    tools.extend(build_invoice_tools(service, ctx))
    tools.extend(build_compliance_tools(service, ctx))
    tools.extend(build_estimate_tools(service))
    return tools


async def _appfolio_auth_check(ctx: ToolContext) -> str | None:
    """Hide AppFolio behind a connect prompt until the user is connected.

    Returns ``None`` (tools available) once a credential is on file. A
    string return surfaces the connect instructions to the agent and
    keeps the data tools out of the LLM schema until then.
    """
    if await is_connected(ctx.user.id):
        return None
    return (
        "AppFolio Vendor Portal is not connected. Tell the user to:"
        " (1) open vendor.appfolio.com and request a magic link,"
        " (2) paste the full link from the email back to you,"
        " (3) you call appfolio_connect with that link."
        " If AppFolio asks for a 2FA code, ask the user for it and"
        " call appfolio_complete_2fa."
    )


def _register() -> None:
    from backend.app.agent.tools.registry import SubToolInfo, default_registry

    default_registry.register(
        _INTEGRATION,
        _appfolio_factory,
        core=False,
        summary=(
            "Manage AppFolio Vendor Portal: view work orders, search,"
            " check payments, and read your vendor profile"
        ),
        sub_tools=[
            SubToolInfo(
                ToolName.APPFOLIO_CONNECT,
                "Connect AppFolio Vendor Portal via magic link",
                default_permission="always",
            ),
            SubToolInfo(
                ToolName.APPFOLIO_COMPLETE_2FA,
                "Submit AppFolio 2FA code",
                default_permission="always",
            ),
            SubToolInfo(
                ToolName.APPFOLIO_LIST_WORK_ORDERS,
                "List AppFolio work orders by status",
                default_permission="always",
            ),
            SubToolInfo(
                ToolName.APPFOLIO_SEARCH_WORK_ORDERS,
                "Search AppFolio work orders by text",
                default_permission="always",
            ),
            SubToolInfo(
                ToolName.APPFOLIO_GET_WORK_ORDER,
                "Get full details for one AppFolio work order",
                default_permission="always",
            ),
            SubToolInfo(
                ToolName.APPFOLIO_LIST_PAYMENTS,
                "List AppFolio payments",
                default_permission="always",
            ),
            SubToolInfo(
                ToolName.APPFOLIO_GET_PROFILE,
                "Get the connected AppFolio profile",
                default_permission="always",
            ),
            SubToolInfo(
                ToolName.APPFOLIO_ACCEPT_WORK_ORDER,
                "Accept an AppFolio work order assignment",
                default_permission="ask",
            ),
            SubToolInfo(
                ToolName.APPFOLIO_SCHEDULE_WORK_ORDER,
                "Schedule an AppFolio work order visit",
                default_permission="ask",
            ),
            SubToolInfo(
                ToolName.APPFOLIO_UPDATE_WORK_ORDER_STATUS,
                "Update the status code on an AppFolio work order",
                default_permission="ask",
            ),
            SubToolInfo(
                ToolName.APPFOLIO_UNDO_WORK_ORDER_STATUS,
                "Revert a recent AppFolio work order status change",
                default_permission="ask",
            ),
            SubToolInfo(
                ToolName.APPFOLIO_LIST_NOTES,
                "List notes on an AppFolio work order",
                default_permission="always",
            ),
            SubToolInfo(
                ToolName.APPFOLIO_ADD_NOTE,
                "Add a note (with optional photos) to an AppFolio work order",
                default_permission="ask",
            ),
            SubToolInfo(
                ToolName.APPFOLIO_UPDATE_NOTE,
                "Edit an existing AppFolio work-order note",
                default_permission="ask",
            ),
            SubToolInfo(
                ToolName.APPFOLIO_MESSAGE_TENANT,
                "Send an SMS to the tenant on an AppFolio work order",
                default_permission="ask",
            ),
            SubToolInfo(
                ToolName.APPFOLIO_CREATE_INVOICE,
                "Build a line-itemized AppFolio invoice with optional photos",
                default_permission="ask",
            ),
            SubToolInfo(
                ToolName.APPFOLIO_UPLOAD_INVOICE_PDF,
                "Upload a pre-built invoice PDF to AppFolio",
                default_permission="ask",
            ),
            SubToolInfo(
                ToolName.APPFOLIO_UPLOAD_COMPLIANCE_DOC,
                "Upload a compliance document (W-9, COI, license)",
                default_permission="ask",
            ),
            SubToolInfo(
                ToolName.APPFOLIO_GET_ESTIMATE,
                "Get an AppFolio estimate's details",
                default_permission="always",
            ),
            SubToolInfo(
                ToolName.APPFOLIO_UPDATE_ESTIMATE,
                "Update an AppFolio estimate amount or description",
                default_permission="ask",
            ),
            SubToolInfo(
                ToolName.APPFOLIO_UPDATE_PROFILE,
                "Update AppFolio profile fields (name, phone, company)",
                default_permission="ask",
            ),
        ],
        auth_check=_appfolio_auth_check,
    )


_register()
