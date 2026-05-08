"""AppFolio Vendor Portal tool registration and factories.

Picked up by the registry's ``ensure_tool_modules_imported`` scan; the
``_register()`` call at the bottom installs two factories at module-import
time:

* ``appfolio_auth`` (core, always on the schema): the magic-link
  ``appfolio_connect`` tool. This must stay reachable even when the user
  has no credential, since pasting the token *is* the connect path.
* ``appfolio_vendor`` (specialist, gated on connection state): the data
  tools (work orders, notes, invoices, payments, etc.). When the user
  is not yet connected, ``_appfolio_vendor_auth_check`` returns a reason
  string so the registry surfaces it under "Not connected" rather than
  letting the LLM believe AppFolio is ready to use.

This split closes a prod bug where the agent confidently told users
"AppFolio is connected" before they had connected, because a single
combined factory had to keep ``auth_check`` returning ``None``
unconditionally to keep the connect tool on the schema.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from backend.app.agent.tools.base import Tool
from backend.app.agent.tools.names import ToolName
from backend.app.config import settings
from backend.app.integrations.appfolio_vendor.auth import load_credential, save_credential
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


_AUTH_FACTORY = "appfolio_auth"
_DATA_FACTORY = "appfolio_vendor"


async def _appfolio_auth_factory(ctx: ToolContext) -> list[Tool]:
    """Assemble just the magic-link auth tools for AppFolio.

    Returns the connect + 2FA tools so the user can authenticate from a
    fresh state. Registered as a core factory so the schema contract is
    independent of credential state. Disabling the user-facing
    ``appfolio_vendor`` integration cascades through ``manage_integration``
    to also flip ``appfolio_auth`` in ``ToolConfigStore``, so the registry's
    ``excluded_factories`` mechanism in ``tool_assembly`` removes both
    factories together without a per-turn DB query in this factory body.
    """
    return list(build_auth_tools(ctx.user.id))


async def _appfolio_vendor_factory(ctx: ToolContext) -> list[Tool]:
    """Assemble the AppFolio data tools for an authenticated user.

    Callers should not invoke this when the user has no credential; the
    registry guards via ``_appfolio_vendor_auth_check`` and skips factory
    creation in that case. The defensive ``return []`` covers the rare
    race where the credential disappears between auth check and create
    (e.g. the user disconnected mid-turn). We log a warning so the race
    is observable rather than silent.
    """
    cred = await load_credential(ctx.user.id)
    if cred is None or not cred.jwt:
        logger.warning(
            "AppFolio credential missing during factory creation for user %s "
            "despite passing auth_check; user may have disconnected mid-turn",
            ctx.user.id,
        )
        return []

    user_id = ctx.user.id

    async def _persist_refreshed(jwt: str, refresh_token: str) -> None:
        await save_credential(
            user_id=user_id,
            jwt=jwt,
            fingerprint=cred.fingerprint,
            customer_ids=cred.customer_ids,
            refresh_token=refresh_token,
        )

    service = build_service(
        cred,
        api_base=settings.appfolio_vendor_api_base,
        on_token_refresh=_persist_refreshed,
    )
    tools: list[Tool] = []
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


async def _appfolio_vendor_auth_check(ctx: ToolContext) -> str | None:
    """Return ``None`` when the user has a usable AppFolio credential.

    When no credential is on file, returns a reason string so the
    registry surfaces ``appfolio_vendor`` under "Not connected" in the
    LLM's capability list. The LLM then knows it must guide the user
    through the magic-link flow before claiming AppFolio access.

    The connect tool itself lives in the separate ``appfolio_auth``
    factory (core, always available), so this auth_check returning a
    reason does not strip the connect path from the schema.
    """
    cred = await load_credential(ctx.user.id)
    if cred is not None and cred.jwt:
        return None
    return (
        "AppFolio Vendor Portal is not connected. Use "
        "manage_integration(action='connect', target='appfolio_vendor') "
        "for the magic-link recipe, then call appfolio_connect with the "
        "URL the user pastes."
    )


def _register() -> None:
    from backend.app.agent.tools.registry import SubToolInfo, default_registry

    default_registry.register(
        _AUTH_FACTORY,
        _appfolio_auth_factory,
        core=True,
        sub_tools=[
            SubToolInfo(
                ToolName.APPFOLIO_CONNECT,
                "Connect AppFolio Vendor Portal via magic link",
                default_permission="always",
            ),
        ],
    )

    default_registry.register(
        _DATA_FACTORY,
        _appfolio_vendor_factory,
        core=False,
        summary=(
            "AppFolio Vendor Portal: view, search, and act on work orders "
            "(accept, schedule, update status, add notes with photos), "
            "message tenants, create or upload invoices, upload compliance "
            "documents (W-9, COI, license), update estimates and profile, "
            "and check payments"
        ),
        sub_tools=[
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
        auth_check=_appfolio_vendor_auth_check,
    )


_register()
