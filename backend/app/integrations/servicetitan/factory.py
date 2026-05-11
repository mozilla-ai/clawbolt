"""ServiceTitan tool registration and factories.

Picked up by the registry's ``ensure_tool_modules_imported`` scan; the
``_register()`` call at the bottom installs two factories at module-import
time, mirroring the AppFolio Vendor split:

* ``servicetitan_auth`` (core, always on the schema): the
  ``connect_servicetitan`` tool. Must stay reachable before any credential
  exists since pasting credentials *is* the connect path.
* ``servicetitan`` (specialist, gated on connection state): the data
  tools. The read tools landed in #1306; the first write tool
  (``st_add_job_note``) landed in #1302. The ``auth_check`` keeps the
  factory surfaced under "Not connected" in ``list_capabilities`` until
  the user runs the connect tool.

The split mirrors AppFolio's prod-bug fix: a single combined factory
would have to keep ``auth_check`` returning ``None`` unconditionally to
preserve the connect tool on the schema, which would tell the LLM the
integration was ready before any credential existed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from backend.app.agent.tools.base import Tool
from backend.app.agent.tools.names import ToolName
from backend.app.agent.tools.servicetitan_tools import build_servicetitan_tools
from backend.app.integrations.servicetitan.auth import is_connected
from backend.app.integrations.servicetitan.auth_tools import build_auth_tools
from backend.app.integrations.servicetitan.service import build_service_for_user

if TYPE_CHECKING:
    from backend.app.agent.tools.registry import ToolContext

logger = logging.getLogger(__name__)


_AUTH_FACTORY = "servicetitan_auth"
_DATA_FACTORY = "servicetitan"


async def _servicetitan_auth_factory(ctx: ToolContext) -> list[Tool]:
    """Assemble the ServiceTitan paste-credentials connect tool.

    Registered as a core factory so the schema contract is independent
    of credential state. The data-side ``servicetitan`` factory is the
    user-facing toggle; this auth factory follows its enable/disable
    state via the registry's hidden-factory pairing (see
    ``_HIDDEN_CORE_FACTORIES`` in ``integration_tools.py`` once #1300
    wires it).
    """
    return list(build_auth_tools(ctx.user.id))


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
    LLM's capability list. The LLM then knows it must guide the user
    through the paste-credentials flow before claiming ServiceTitan
    access.

    The connect tool itself lives in the separate ``servicetitan_auth``
    factory (core, always available), so this auth_check returning a
    reason does not strip the connect path from the schema.
    """
    if await is_connected(ctx.user.id):
        return None
    return (
        "ServiceTitan is not connected. Ask the user to paste their Tenant ID,"
        " Client ID, and Client Secret, then call connect_servicetitan to"
        " validate and persist them."
    )


def _register() -> None:
    from backend.app.agent.tools.registry import SubToolInfo, default_registry

    default_registry.register(
        _AUTH_FACTORY,
        _servicetitan_auth_factory,
        core=True,
        sub_tools=[
            SubToolInfo(
                ToolName.SERVICETITAN_CONNECT,
                "Connect a ServiceTitan tenant by pasting Tenant ID, Client ID, and Client Secret",
                default_permission="always",
            ),
        ],
    )

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
