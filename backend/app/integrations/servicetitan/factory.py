"""ServiceTitan tool registration and factories.

Picked up by the registry's ``ensure_tool_modules_imported`` scan; the
``_register()`` call at the bottom installs two factories at module-import
time, mirroring the AppFolio Vendor split:

* ``servicetitan_auth`` (core, always on the schema): the
  ``connect_servicetitan`` tool. Must stay reachable before any credential
  exists since pasting credentials *is* the connect path.
* ``servicetitan`` (specialist, gated on connection state): the data
  tools, populated in the read-tools issue (#1300). For now the factory
  returns an empty tool list; the ``auth_check`` keeps it surfaced
  under "Not connected" in ``list_capabilities`` until the user runs
  the connect tool.

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
from backend.app.integrations.servicetitan.auth import is_connected
from backend.app.integrations.servicetitan.auth_tools import build_auth_tools

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

    Empty for now: the read tools land in #1300 and the write tools in
    #1301. The factory still exists so ``list_capabilities`` and the
    Settings page know the integration is real; ``auth_check`` keeps
    it surfaced as "not connected" until the user pastes credentials.
    """
    if not await is_connected(ctx.user.id):
        return []
    # Resource-tool builders land in subsequent issues and will be wired
    # in here once they exist. Keep the factory body shaped like
    # AppFolio's so adding builders later is a one-line edit.
    return []


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
            "ServiceTitan: view and act on customers, jobs, and appointments"
            " (read-only for now; write tools land in a subsequent issue)."
        ),
        display_name="ServiceTitan",
        dashboard_description=("View ServiceTitan customers, jobs, and appointments"),
        dashboard_group="Integrations",
        dashboard_group_order=3,
        sub_tools=[],
        auth_check=_servicetitan_auth_check,
    )


_register()
