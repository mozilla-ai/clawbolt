"""AppFolio Vendor Portal tool registration and factories.

Picked up by the registry's ``ensure_tool_modules_imported`` scan; the
``_register()`` call at the bottom installs the ``appfolio_vendor``
specialist factory at module-import time: the data tools (work-order reads +
status updates, notes with photos, invoices). When the user is not yet
connected, ``_appfolio_vendor_auth_check`` returns a reason string so the
registry surfaces it under "Not connected" rather than letting the LLM
believe AppFolio is ready to use. That closed a prod bug where the agent
told users "AppFolio is connected" before they had connected.

Connecting happens in the Clawbolt web app, not over chat: the magic link
is a single-use secret, and pasting it into a chat thread would leave it in
the user's message history (issue #1337). The connect form lives on the
Settings page and posts to ``/api/integrations/appfolio_vendor``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from backend.app.agent.tools.base import Tool
from backend.app.agent.tools.names import ToolName
from backend.app.config import settings
from backend.app.integrations.appfolio_vendor.auth import load_credential, save_credential
from backend.app.integrations.appfolio_vendor.invoices import build_invoice_tools
from backend.app.integrations.appfolio_vendor.notes import build_note_tools
from backend.app.integrations.appfolio_vendor.service import build_service
from backend.app.integrations.appfolio_vendor.work_order_writes import (
    build_work_order_write_tools,
)
from backend.app.integrations.appfolio_vendor.work_orders import build_work_order_tools

if TYPE_CHECKING:
    from backend.app.agent.tools.registry import ToolContext

logger = logging.getLogger(__name__)


_DATA_FACTORY = "appfolio_vendor"


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
    tools.extend(build_invoice_tools(service, ctx))
    return tools


async def _appfolio_vendor_auth_check(ctx: ToolContext) -> str | None:
    """Return ``None`` when the user has a usable AppFolio credential.

    When no credential is on file, returns a reason string so the
    registry surfaces ``appfolio_vendor`` under "Not connected" in the
    LLM's capability list. The reason routes the user to the web app: the
    single-use magic link must never be pasted into chat (issue #1337).
    """
    cred = await load_credential(ctx.user.id)
    if cred is not None and cred.jwt:
        return None
    return (
        "AppFolio Vendor Portal is not connected. The user connects it in the"
        " Clawbolt web app under Settings, where they paste the magic link from"
        " their AppFolio sign-in email. Do not accept the magic link over chat;"
        " direct the user to the web app instead."
    )


def _register() -> None:
    from backend.app.agent.tools.registry import SubToolInfo, default_registry

    default_registry.register(
        _DATA_FACTORY,
        _appfolio_vendor_factory,
        core=False,
        summary=(
            "AppFolio Vendor Portal: view and search work orders, update their "
            "status (e.g. mark complete), read and add notes (with photos), "
            "and create or upload invoices"
        ),
        display_name="AppFolio Vendor Portal",
        dashboard_description=(
            "View work orders, update status, add notes, and create invoices "
            "in AppFolio Vendor Portal"
        ),
        dashboard_group="Integrations",
        dashboard_group_order=2,
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
                ToolName.APPFOLIO_CREATE_INVOICE,
                "Build a line-itemized AppFolio invoice with optional photos",
                default_permission="ask",
            ),
            SubToolInfo(
                ToolName.APPFOLIO_UPLOAD_INVOICE_PDF,
                "Upload a pre-built invoice PDF to AppFolio",
                default_permission="ask",
            ),
        ],
        auth_check=_appfolio_vendor_auth_check,
    )


_register()
