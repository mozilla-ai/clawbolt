"""CompanyCam tool registration and factory.

This module is the entrypoint for tool auto-discovery (the ``factory``
module is picked up by ``ensure_tool_modules_imported``). It wires
together the grouped implementation modules:

* ``params``      -- Pydantic parameter models
* ``projects``    -- project lifecycle + notepad + documents
* ``photos``      -- photo upload/tag/delete/search + comments
* ``checklists``  -- checklist management

Authentication uses the standard OAuth 2.0 authorization code flow
(same as Google Calendar and QuickBooks).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from backend.app.agent.tools.base import Tool
from backend.app.agent.tools.names import ToolName
from backend.app.config import settings
from backend.app.integrations.companycam.checklists import build_checklist_tools
from backend.app.integrations.companycam.photos import build_photo_tools
from backend.app.integrations.companycam.projects import build_project_tools
from backend.app.integrations.companycam.service import CompanyCamService
from backend.app.services.oauth import oauth_service

if TYPE_CHECKING:
    from backend.app.agent.tools.registry import ToolContext

logger = logging.getLogger(__name__)

_INTEGRATION = "companycam"


async def _load_service(user_id: str) -> CompanyCamService | None:
    """Load a CompanyCamService for the user using OAuth token (auto-refreshed)."""
    token = await oauth_service.get_valid_token(user_id, _INTEGRATION)
    if token and token.access_token:
        return CompanyCamService(access_token=token.access_token)
    return None


def _create_companycam_tools(service: CompanyCamService, ctx: ToolContext) -> list[Tool]:
    """Assemble the full CompanyCam tool list from the domain modules.

    Only ``build_photo_tools`` needs the ``ToolContext`` (for the user's
    downloaded media staging); project and checklist tools talk purely
    to the CompanyCam service.
    """
    return [
        *build_project_tools(service),
        *build_photo_tools(service, ctx),
        *build_checklist_tools(service),
    ]


def _companycam_auth_check(ctx: ToolContext) -> str | None:
    """Check whether CompanyCam is available for this user.

    Returns None when connected (tools are available).
    Returns a reason string when not connected (tells the agent how to help).
    """
    if not settings.companycam_client_id or not settings.companycam_client_secret:
        return None  # Not configured server-side, hide tools
    if oauth_service.is_connected(ctx.user.id, _INTEGRATION):
        return None
    return (
        "CompanyCam is not connected. "
        "Use manage_integration(action='connect', target='companycam') "
        "to start the OAuth authorization flow."
    )


async def _companycam_factory(ctx: ToolContext) -> list[Tool]:
    """Factory for CompanyCam tools."""
    if not settings.companycam_client_id or not settings.companycam_client_secret:
        return []

    service = await _load_service(ctx.user.id)
    if service is None:
        return []
    return _create_companycam_tools(service, ctx)


def _register() -> None:
    from backend.app.agent.tools.registry import SubToolInfo, default_registry

    default_registry.register(
        "companycam",
        _companycam_factory,
        core=False,
        summary=(
            "Manage job site documentation with CompanyCam: photos, projects, "
            "documents, comments, checklists, and tags"
        ),
        sub_tools=[
            SubToolInfo(
                ToolName.COMPANYCAM_SEARCH_PROJECTS,
                "Search CompanyCam projects by name or address",
                default_permission="always",
            ),
            SubToolInfo(
                ToolName.COMPANYCAM_CREATE_PROJECT,
                "Create a new CompanyCam project",
                default_permission="ask",
            ),
            SubToolInfo(
                ToolName.COMPANYCAM_UPDATE_PROJECT,
                "Update a CompanyCam project name or address",
                default_permission="ask",
            ),
            SubToolInfo(
                ToolName.COMPANYCAM_UPLOAD_PHOTO,
                "Upload a photo to a CompanyCam project",
                default_permission="ask",
            ),
            SubToolInfo(
                ToolName.COMPANYCAM_GET_PROJECT,
                "Get full project details",
                default_permission="always",
            ),
            SubToolInfo(
                ToolName.COMPANYCAM_ARCHIVE_PROJECT,
                "Archive a completed project",
                default_permission="ask",
            ),
            SubToolInfo(
                ToolName.COMPANYCAM_DELETE_PROJECT,
                "Permanently delete a project",
                default_permission="ask",
            ),
            SubToolInfo(
                ToolName.COMPANYCAM_UPDATE_NOTEPAD,
                "Update project notes",
                default_permission="ask",
            ),
            SubToolInfo(
                ToolName.COMPANYCAM_LIST_DOCUMENTS,
                "List project documents",
                default_permission="always",
            ),
            SubToolInfo(
                ToolName.COMPANYCAM_ADD_COMMENT,
                "Add a comment to a project or photo",
                default_permission="ask",
            ),
            SubToolInfo(
                ToolName.COMPANYCAM_LIST_COMMENTS,
                "List comments on a project or photo",
                default_permission="always",
            ),
            SubToolInfo(
                ToolName.COMPANYCAM_TAG_PHOTO,
                "Add tags to a photo",
                default_permission="ask",
            ),
            SubToolInfo(
                ToolName.COMPANYCAM_DELETE_PHOTO,
                "Permanently delete a photo",
                default_permission="ask",
            ),
            SubToolInfo(
                ToolName.COMPANYCAM_SEARCH_PHOTOS,
                "Search photos across all projects",
                default_permission="always",
            ),
            SubToolInfo(
                ToolName.COMPANYCAM_LIST_CHECKLISTS,
                "List project checklists",
                default_permission="always",
            ),
            SubToolInfo(
                ToolName.COMPANYCAM_GET_CHECKLIST,
                "Get checklist details with tasks",
                default_permission="always",
            ),
            SubToolInfo(
                ToolName.COMPANYCAM_CREATE_CHECKLIST,
                "Create a checklist from a template",
                default_permission="ask",
            ),
        ],
        auth_check=_companycam_auth_check,
    )


_register()
