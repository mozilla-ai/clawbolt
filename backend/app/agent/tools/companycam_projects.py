"""CompanyCam project-management tools.

Implements the project lifecycle (search, create, update, get, archive,
delete), the notepad, and document listing. Built by the factory in
``companycam_tools`` which passes the authenticated service and context.
"""

from __future__ import annotations

import logging

from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolReceipt, ToolResult
from backend.app.agent.tools.companycam_params import (
    CompanyCamArchiveProjectParams,
    CompanyCamCreateProjectParams,
    CompanyCamDeleteProjectParams,
    CompanyCamGetProjectParams,
    CompanyCamListDocumentsParams,
    CompanyCamSearchParams,
    CompanyCamUpdateNotepadParams,
    CompanyCamUpdateProjectParams,
)
from backend.app.agent.tools.companycam_receipts import (
    project_target,
    project_url,
)
from backend.app.agent.tools.names import ToolName
from backend.app.services.companycam import CompanyCamService

logger = logging.getLogger(__name__)


def build_project_tools(service: CompanyCamService) -> list[Tool]:
    """Return the CompanyCam project-management Tool instances.

    These tools only interact with the CompanyCam service, so the
    builder does not need the agent's ``ToolContext``.
    """

    async def companycam_search_projects(query: str) -> ToolResult:
        """Search CompanyCam projects by name or address."""
        try:
            projects = await service.search_projects(query)
        except Exception as exc:
            logger.exception("CompanyCam search failed: %s", exc)
            return ToolResult(
                content=f"CompanyCam search error: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

        if not projects:
            return ToolResult(content=f"No CompanyCam projects found for '{query}'.")

        lines = [f"Found {len(projects)} project(s):"]
        for p in projects[:20]:
            addr_str = p.address.street_address_1 if p.address else ""
            lines.append(
                f"- ID: {p.id} | {p.name or 'Untitled'}" + (f" | {addr_str}" if addr_str else "")
            )
        return ToolResult(content="\n".join(lines))

    async def companycam_create_project(name: str, address: str = "") -> ToolResult:
        """Create a new CompanyCam project."""
        try:
            project = await service.create_project(name, address)
        except Exception as exc:
            logger.exception("CompanyCam create project failed: %s", exc)
            return ToolResult(
                content=f"CompanyCam error creating project: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

        return ToolResult(
            content=(f"Created CompanyCam project: {project.name or name} (ID: {project.id})"),
            receipt=ToolReceipt(
                action="Created CompanyCam project",
                target=project_target(project),
                url=project.project_url or project_url(project.id),
            ),
        )

    async def companycam_update_project(
        project_id: str,
        name: str = "",
        address: str = "",
    ) -> ToolResult:
        """Update a CompanyCam project's name or address."""
        if not name and not address:
            return ToolResult(
                content="Provide a new name or address to update.",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )
        try:
            project = await service.update_project(
                project_id,
                name=name or None,
                address=address or None,
            )
        except Exception as exc:
            logger.exception("CompanyCam update project failed: %s", exc)
            return ToolResult(
                content=f"CompanyCam error updating project: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

        return ToolResult(
            content=f"Updated CompanyCam project: {project.name or ''} (ID: {project_id})",
            receipt=ToolReceipt(
                action="Updated CompanyCam project",
                target=project_target(project),
                url=project.project_url or project_url(project_id),
            ),
        )

    async def companycam_get_project(project_id: str) -> ToolResult:
        """Get full details for a CompanyCam project."""
        try:
            project = await service.get_project(project_id)
        except Exception as exc:
            logger.exception("CompanyCam get project failed: %s", exc)
            return ToolResult(
                content=f"CompanyCam error: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

        lines = [f"Project: {project.name or 'Untitled'} (ID: {project.id})"]
        if project.address:
            addr = project.address
            parts = [p for p in [addr.street_address_1, addr.city, addr.state] if p]
            if parts:
                lines.append(f"Address: {', '.join(parts)}")
        lines.append(f"Status: {project.status or 'unknown'}")
        lines.append(f"Archived: {project.archived or False}")
        if project.notepad:
            lines.append(f"Notepad: {project.notepad}")
        if project.primary_contact:
            contact = project.primary_contact
            contact_parts = [contact.name or ""]
            if contact.phone_number:
                contact_parts.append(contact.phone_number)
            if contact.email:
                contact_parts.append(contact.email)
            lines.append(f"Contact: {' | '.join(p for p in contact_parts if p)}")
        if project.project_url:
            lines.append(f"URL: {project.project_url}")
        return ToolResult(content="\n".join(lines))

    async def companycam_archive_project(project_id: str) -> ToolResult:
        """Archive a completed CompanyCam project."""
        try:
            await service.archive_project(project_id)
        except Exception as exc:
            logger.exception("CompanyCam archive project failed: %s", exc)
            return ToolResult(
                content=f"CompanyCam error: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )
        return ToolResult(
            content=f"Project {project_id} archived successfully.",
            receipt=ToolReceipt(
                action="Archived CompanyCam project",
                target=project_target(None),
                url=project_url(project_id),
            ),
        )

    async def companycam_delete_project(project_id: str) -> ToolResult:
        """Permanently delete a CompanyCam project. Cannot be undone."""
        try:
            await service.delete_project(project_id)
        except Exception as exc:
            logger.exception("CompanyCam delete project failed: %s", exc)
            return ToolResult(
                content=f"CompanyCam error: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )
        return ToolResult(
            content=f"Project {project_id} permanently deleted.",
            receipt=ToolReceipt(
                action="Deleted CompanyCam project",
                target=project_target(None),
            ),
        )

    async def companycam_update_notepad(
        project_id: str,
        notepad: str,
    ) -> ToolResult:
        """Update the notepad on a CompanyCam project."""
        try:
            await service.update_notepad(project_id, notepad)
        except Exception as exc:
            logger.exception("CompanyCam update notepad failed: %s", exc)
            return ToolResult(
                content=f"CompanyCam error: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )
        return ToolResult(
            content=f"Notepad updated on project {project_id}.",
            receipt=ToolReceipt(
                action="Updated notepad on CompanyCam project",
                target=project_target(None),
                url=project_url(project_id),
            ),
        )

    async def companycam_list_documents(
        project_id: str,
        page: int = 1,
    ) -> ToolResult:
        """List documents attached to a CompanyCam project."""
        try:
            docs = await service.list_project_documents(project_id, page=page)
        except Exception as exc:
            logger.exception("CompanyCam list documents failed: %s", exc)
            return ToolResult(
                content=f"CompanyCam error: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )
        if not docs:
            return ToolResult(content="No documents found on this project.")
        lines = [f"Found {len(docs)} document(s):"]
        for d in docs:
            size = f" ({d.byte_size} bytes)" if d.byte_size else ""
            lines.append(f"- {d.name or 'Untitled'}{size}: {d.url or 'no URL'}")
        if len(docs) >= 50:
            lines.append(f"(Page {page}. More results may be available on the next page.)")
        return ToolResult(content="\n".join(lines))

    return [
        Tool(
            name=ToolName.COMPANYCAM_SEARCH_PROJECTS,
            description="Search CompanyCam projects by name or address",
            function=companycam_search_projects,
            params_model=CompanyCamSearchParams,
            usage_hint=(
                "Search for a CompanyCam project before uploading photos. "
                "Use the client address or name as the search query."
            ),
        ),
        Tool(
            name=ToolName.COMPANYCAM_CREATE_PROJECT,
            description="Create a new CompanyCam project",
            function=companycam_create_project,
            params_model=CompanyCamCreateProjectParams,
            usage_hint=(
                "Create a new project when no matching project exists. "
                "Use the client name and address as the project name."
            ),
        ),
        Tool(
            name=ToolName.COMPANYCAM_UPDATE_PROJECT,
            description="Update a CompanyCam project's name or address",
            function=companycam_update_project,
            params_model=CompanyCamUpdateProjectParams,
            usage_hint=(
                "Use to rename a project or update its address. "
                "For example, adding a client name to a project."
            ),
        ),
        Tool(
            name=ToolName.COMPANYCAM_GET_PROJECT,
            description="Get full details for a CompanyCam project",
            function=companycam_get_project,
            params_model=CompanyCamGetProjectParams,
            usage_hint=(
                "Use to check project details including address, notepad, "
                "contacts, and status. Search for the project first to get the ID."
            ),
        ),
        Tool(
            name=ToolName.COMPANYCAM_ARCHIVE_PROJECT,
            description="Archive a completed CompanyCam project",
            function=companycam_archive_project,
            params_model=CompanyCamArchiveProjectParams,
            usage_hint="Archive a project when a job is completed.",
        ),
        Tool(
            name=ToolName.COMPANYCAM_DELETE_PROJECT,
            description=(
                "WARNING: Permanently delete a CompanyCam project. "
                "This cannot be undone. Consider archiving instead."
            ),
            function=companycam_delete_project,
            params_model=CompanyCamDeleteProjectParams,
            usage_hint=(
                "Only delete a project if the user explicitly asks. Suggest archiving first."
            ),
        ),
        Tool(
            name=ToolName.COMPANYCAM_UPDATE_NOTEPAD,
            description="Update the notepad (notes) on a CompanyCam project",
            function=companycam_update_notepad,
            params_model=CompanyCamUpdateNotepadParams,
            usage_hint="Add or update notes on a project.",
        ),
        Tool(
            name=ToolName.COMPANYCAM_LIST_DOCUMENTS,
            description="List documents attached to a CompanyCam project",
            function=companycam_list_documents,
            params_model=CompanyCamListDocumentsParams,
            usage_hint="Check what contracts, specs, or files are attached to a project.",
        ),
    ]
