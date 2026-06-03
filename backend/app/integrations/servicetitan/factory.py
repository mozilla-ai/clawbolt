"""ServiceTitan tool registration and factories.

Picked up by the registry's ``ensure_tool_modules_imported`` scan; the
``_register()`` call at the bottom installs the ``servicetitan`` specialist
factory at module-import time. The read tools landed in #1306; the first
write tool (``st_add_job_note``) landed in #1302. The ``auth_check`` keeps
the factory surfaced under "Not connected" in ``list_capabilities`` until
the user connects.

Connecting happens in the Clawbolt web app, not over chat: the tenant's
Client Secret is a credential, and pasting it into a chat thread would
leave it in the user's message history (issue #1337). The connect form
lives on the Settings page and posts to ``/api/integrations/servicetitan``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from backend.app.agent.tools.base import Tool
from backend.app.agent.tools.names import ToolName
from backend.app.agent.tools.servicetitan_tools import build_servicetitan_tools
from backend.app.integrations.servicetitan.auth import is_connected
from backend.app.integrations.servicetitan.service import build_service_for_user

if TYPE_CHECKING:
    from backend.app.agent.tools.registry import ToolContext

logger = logging.getLogger(__name__)


_DATA_FACTORY = "servicetitan"


async def _servicetitan_factory(ctx: ToolContext) -> list[Tool]:
    """Assemble the ServiceTitan data tools for an authenticated user.

    Returns the read and write tools when the user has a usable
    credential on file. The defensive ``return []`` after
    ``build_service_for_user`` covers the rare race where the credential
    disappears between the auth check and tool creation (user
    disconnected mid-turn).
    """
    if not await is_connected(ctx.user.id):
        return []
    service = await build_service_for_user(ctx.user.id)
    if service is None:
        return []
    return list(build_servicetitan_tools(service))


async def _servicetitan_auth_check(ctx: ToolContext) -> str | None:
    """Return ``None`` when the user has a usable ServiceTitan credential.

    When no credential is on file, returns a reason string so the
    registry surfaces ``servicetitan`` under "Not connected" in the
    LLM's capability list. The reason routes the user to the web app:
    the Client Secret must never be pasted into chat (issue #1337).
    """
    if await is_connected(ctx.user.id):
        return None
    return (
        "ServiceTitan is not connected. The user connects it in the Clawbolt"
        " web app under Settings, where they paste their Tenant ID, Client ID,"
        " and Client Secret. Do not accept those secrets over chat; direct the"
        " user to the web app instead."
    )


def _register() -> None:
    from backend.app.agent.tools.registry import SubToolInfo, default_registry

    default_registry.register(
        _DATA_FACTORY,
        _servicetitan_factory,
        core=False,
        summary=(
            "ServiceTitan: view customers, jobs, and appointments; add"
            " notes to jobs. Note writes require user approval."
        ),
        display_name="ServiceTitan",
        dashboard_description=(
            "View ServiceTitan customers, jobs, and appointments; add notes to jobs"
        ),
        dashboard_group="Integrations",
        dashboard_group_order=3,
        sub_tools=[
            SubToolInfo(
                ToolName.SERVICETITAN_SEARCH_CUSTOMERS,
                "Search ServiceTitan customers by name or phone",
                default_permission="always",
            ),
            SubToolInfo(
                ToolName.SERVICETITAN_GET_CUSTOMER,
                "Fetch a ServiceTitan customer record by id",
                default_permission="always",
            ),
            SubToolInfo(
                ToolName.SERVICETITAN_LIST_APPOINTMENTS,
                "List ServiceTitan appointments in a date range",
                default_permission="always",
            ),
            SubToolInfo(
                ToolName.SERVICETITAN_ADD_JOB_NOTE,
                "Add a note to a ServiceTitan job",
                default_permission="ask",
            ),
        ],
        auth_check=_servicetitan_auth_check,
    )


_register()
