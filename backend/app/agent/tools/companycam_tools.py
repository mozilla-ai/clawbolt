"""CompanyCam tools for the agent.

Provides tools to connect to CompanyCam, search/create projects, and upload
photos. The connection uses a per-user API token stored in the oauth_tokens
table (OAuth 2.0 support is planned post-MVP).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.services.companycam import CompanyCamService, get_photo_url
from backend.app.services.oauth import OAuthTokenData, oauth_service

if TYPE_CHECKING:
    from backend.app.agent.tools.registry import ToolContext

logger = logging.getLogger(__name__)

_INTEGRATION = "companycam"


# ---------------------------------------------------------------------------
# Parameter models
# ---------------------------------------------------------------------------


class CompanyCamConnectParams(BaseModel):
    api_token: str = Field(description="The user's CompanyCam API access token")


class CompanyCamSearchParams(BaseModel):
    query: str = Field(description="Search term: project name, address, or keyword")


class CompanyCamCreateProjectParams(BaseModel):
    name: str = Field(description="Project name (typically client name and address)")
    address: str = Field(default="", description="Street address for the project")


class CompanyCamUpdateProjectParams(BaseModel):
    project_id: str = Field(description="CompanyCam project ID to update")
    name: str = Field(default="", description="New project name (leave empty to keep current)")
    address: str = Field(default="", description="New street address (leave empty to keep current)")


class CompanyCamUploadPhotoParams(BaseModel):
    project_id: str = Field(description="CompanyCam project ID to upload to")
    original_url: str = Field(
        default="",
        description=(
            "The original_url of a photo from the current conversation. "
            "If empty, uploads the most recent photo."
        ),
    )
    description: str = Field(default="", description="Photo description")
    tags: list[str] = Field(default_factory=list, description="Tags to apply to the photo")


class CompanyCamGetProjectParams(BaseModel):
    project_id: str = Field(description="CompanyCam project ID")


class CompanyCamArchiveProjectParams(BaseModel):
    project_id: str = Field(description="CompanyCam project ID to archive")


class CompanyCamDeleteProjectParams(BaseModel):
    project_id: str = Field(description="CompanyCam project ID to permanently delete")


class CompanyCamUpdateNotepadParams(BaseModel):
    project_id: str = Field(description="CompanyCam project ID")
    notepad: str = Field(description="New notepad content for the project")


class CompanyCamListDocumentsParams(BaseModel):
    project_id: str = Field(description="CompanyCam project ID")
    page: int = Field(default=1, description="Page number (default 1)")


class CompanyCamAddCommentParams(BaseModel):
    target_type: str = Field(description="Type of target: 'project' or 'photo'")
    target_id: str = Field(description="ID of the project or photo to comment on")
    content: str = Field(description="Comment text")


class CompanyCamListCommentsParams(BaseModel):
    target_type: str = Field(description="Type of target: 'project' or 'photo'")
    target_id: str = Field(description="ID of the project or photo")
    page: int = Field(default=1, description="Page number (default 1)")


class CompanyCamTagPhotoParams(BaseModel):
    photo_id: str = Field(description="CompanyCam photo ID to tag")
    tags: list[str] = Field(description="Tags to add to the photo")


class CompanyCamDeletePhotoParams(BaseModel):
    photo_id: str = Field(description="CompanyCam photo ID to permanently delete")


class CompanyCamSearchPhotosParams(BaseModel):
    project_id: str = Field(
        default="",
        description="Optional: filter to a specific project ID",
    )
    start_date: str = Field(
        default="",
        description="Optional: start date filter (ISO format, e.g. 2024-01-15)",
    )
    end_date: str = Field(
        default="",
        description="Optional: end date filter (ISO format, e.g. 2024-01-31)",
    )
    page: int = Field(default=1, description="Page number (default 1)")


class CompanyCamListChecklistsParams(BaseModel):
    project_id: str = Field(description="CompanyCam project ID")


class CompanyCamGetChecklistParams(BaseModel):
    project_id: str = Field(description="CompanyCam project ID")
    checklist_id: str = Field(description="Checklist ID to retrieve")


class CompanyCamCreateChecklistParams(BaseModel):
    project_id: str = Field(description="CompanyCam project ID")
    template_id: str = Field(
        description="Checklist template ID to create from (use list_checklists to find templates)"
    )


# ---------------------------------------------------------------------------
# Service loader
# ---------------------------------------------------------------------------


def _load_service(user_id: str) -> CompanyCamService | None:
    """Load a CompanyCamService for the user.

    Priority: per-user token (oauth_tokens) > server-level env var.
    """
    from backend.app.config import settings

    token = oauth_service.load_token(user_id, _INTEGRATION)
    if token and token.access_token:
        return CompanyCamService(access_token=token.access_token)
    if settings.companycam_access_token:
        return CompanyCamService(access_token=settings.companycam_access_token)
    return None


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def _create_companycam_tools(
    service: CompanyCamService,
    ctx: ToolContext,
) -> list[Tool]:
    """Build the CompanyCam tool list with the given service and context."""

    from backend.app.agent import media_staging

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
        )

    async def companycam_upload_photo(
        project_id: str,
        original_url: str = "",
        description: str = "",
        tags: list[str] | None = None,
    ) -> ToolResult:
        """Upload a photo from the current conversation to a CompanyCam project."""
        from backend.app.agent.stores import MediaStore
        from backend.app.config import settings
        from backend.app.routers.media_temp import create_temp_media_url

        # Sanitize LLM-supplied tags
        if tags:
            tags = [t.strip()[:50] for t in tags[:10] if t.strip()]

        file_bytes: bytes = b""
        mime_type = "image/jpeg"
        photo_uri = ""

        # 1. Try downloaded_media (current message, not yet evicted)
        for media in ctx.downloaded_media:
            if original_url and media.original_url != original_url:
                continue
            file_bytes = media.content
            mime_type = media.mime_type or "image/jpeg"
            original_url = original_url or media.original_url
            break

        # 2. Try media staging (cached bytes, may have been evicted)
        if not file_bytes:
            all_staged = media_staging.get_all_for_user(ctx.user.id)
            if original_url and original_url in all_staged:
                file_bytes = all_staged[original_url]
            elif all_staged:
                first_url = next(iter(all_staged))
                file_bytes = all_staged[first_url]
                original_url = original_url or first_url

        # 3. Fall back to MediaFile records (already saved to storage)
        if not file_bytes:
            media_store = MediaStore(ctx.user.id)
            media_file = None
            if original_url:
                media_file = await media_store.get_by_url(original_url)
            if media_file is None:
                all_media = await media_store.list_all()
                if all_media:
                    media_file = all_media[-1]

            if media_file:
                mime_type = media_file.mime_type or "image/jpeg"
                storage_url = media_file.storage_url
                # Cloud storage: use the shareable URL directly
                if storage_url and not storage_url.startswith("file://"):
                    photo_uri = storage_url
                # Local storage: read bytes from disk
                elif storage_url and storage_url.startswith("file://"):
                    local_path = Path(storage_url.removeprefix("file://"))
                    if local_path.is_file():
                        file_bytes = await asyncio.to_thread(local_path.read_bytes)

        if not file_bytes and not photo_uri:
            return ToolResult(
                content=(
                    "No photo available to upload. Send a photo in the "
                    "conversation, or save one to storage first."
                ),
                is_error=True,
                error_kind=ToolErrorKind.NOT_FOUND,
            )

        # Build the URI for CompanyCam to download.
        # Must be publicly accessible. Try the Cloudflare tunnel first
        # (app_base_url may be a private Tailscale/LAN address).
        if not photo_uri:
            from backend.app.services.webhook import discover_tunnel_url

            public_base = await discover_tunnel_url(max_retries=1, delay=0)
            base_url = public_base or settings.app_base_url
            photo_uri = create_temp_media_url(file_bytes, mime_type, base_url)

        try:
            photo = await service.upload_photo(
                project_id=project_id,
                photo_uri=photo_uri,
                tags=tags,
                description=description,
            )
        except Exception as exc:
            logger.exception("CompanyCam upload failed: %s", exc)
            return ToolResult(
                content=f"CompanyCam upload error: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

        # Poll for processing status (CompanyCam downloads the image async)
        status = photo.processing_status or "pending"
        if status == "pending":
            for _ in range(3):
                await asyncio.sleep(2)
                try:
                    photo = await service.get_photo(photo.id)
                    status = photo.processing_status or "unknown"
                    if status != "pending":
                        break
                except Exception:
                    break

        url = get_photo_url(photo)
        logger.info(
            "CompanyCam photo result: project=%s id=%s status=%s url=%s",
            project_id,
            photo.id,
            status,
            url,
        )

        if status == "processing_error":
            return ToolResult(
                content=(
                    f"CompanyCam accepted the upload but failed to process the photo "
                    f"(ID: {photo.id}). This usually means CompanyCam could not "
                    f"download the image from the temporary URL. Check that the "
                    f"server is publicly accessible."
                ),
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

        if status == "duplicate":
            return ToolResult(
                content=f"CompanyCam detected this as a duplicate photo (ID: {photo.id}).",
            )

        status_note = ""
        if status == "pending":
            status_note = " (still processing, may take a moment to appear)"
        return ToolResult(
            content=f"Photo uploaded to CompanyCam project {project_id}: {url}{status_note}",
        )

    # ------------------------------------------------------------------
    # New tools: project management
    # ------------------------------------------------------------------

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
        return ToolResult(content=f"Project {project_id} archived successfully.")

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
        return ToolResult(content=f"Project {project_id} permanently deleted.")

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
        )

    # ------------------------------------------------------------------
    # New tools: project content
    # ------------------------------------------------------------------

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

    async def companycam_add_comment(
        target_type: str,
        target_id: str,
        content: str,
    ) -> ToolResult:
        """Add a comment to a CompanyCam project or photo."""
        if target_type not in ("project", "photo"):
            return ToolResult(
                content="target_type must be 'project' or 'photo'.",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )
        try:
            if target_type == "project":
                comment = await service.add_project_comment(target_id, content)
            else:
                comment = await service.add_photo_comment(target_id, content)
        except Exception as exc:
            logger.exception("CompanyCam add comment failed: %s", exc)
            return ToolResult(
                content=f"CompanyCam error: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )
        return ToolResult(
            content=f"Comment added to {target_type} {target_id} (ID: {comment.id}).",
        )

    async def companycam_list_comments(
        target_type: str,
        target_id: str,
        page: int = 1,
    ) -> ToolResult:
        """List comments on a CompanyCam project or photo."""
        if target_type not in ("project", "photo"):
            return ToolResult(
                content="target_type must be 'project' or 'photo'.",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )
        try:
            if target_type == "project":
                comments = await service.list_project_comments(target_id, page=page)
            else:
                comments = await service.list_photo_comments(target_id, page=page)
        except Exception as exc:
            logger.exception("CompanyCam list comments failed: %s", exc)
            return ToolResult(
                content=f"CompanyCam error: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )
        if not comments:
            return ToolResult(content=f"No comments on this {target_type}.")
        lines = [f"Found {len(comments)} comment(s):"]
        for c in comments:
            author = c.creator_name or "Unknown"
            lines.append(f"- [{author}]: {c.content or ''}")
        if len(comments) >= 50:
            lines.append(f"(Page {page}. More results may be available on the next page.)")
        return ToolResult(content="\n".join(lines))

    # ------------------------------------------------------------------
    # New tools: photo management
    # ------------------------------------------------------------------

    async def companycam_tag_photo(
        photo_id: str,
        tags: list[str] | None = None,
    ) -> ToolResult:
        """Add tags to a CompanyCam photo."""
        if not tags:
            return ToolResult(
                content="Provide at least one tag.",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )
        clean_tags = [t.strip()[:50] for t in tags[:10] if t.strip()]
        if not clean_tags:
            return ToolResult(
                content="No valid tags provided.",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )
        try:
            result_tags = await service.add_photo_tags(photo_id, clean_tags)
        except Exception as exc:
            logger.exception("CompanyCam tag photo failed: %s", exc)
            return ToolResult(
                content=f"CompanyCam error: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )
        tag_names = [t.display_value or t.value or "?" for t in result_tags]
        return ToolResult(
            content=f"Tagged photo {photo_id} with: {', '.join(tag_names)}",
        )

    async def companycam_delete_photo(photo_id: str) -> ToolResult:
        """Permanently delete a CompanyCam photo. Cannot be undone."""
        try:
            await service.delete_photo(photo_id)
        except Exception as exc:
            logger.exception("CompanyCam delete photo failed: %s", exc)
            return ToolResult(
                content=f"CompanyCam error: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )
        return ToolResult(content=f"Photo {photo_id} permanently deleted.")

    async def companycam_search_photos(
        project_id: str = "",
        start_date: str = "",
        end_date: str = "",
        page: int = 1,
    ) -> ToolResult:
        """Search photos across all CompanyCam projects."""
        import calendar
        from datetime import datetime

        start_ts: int | None = None
        end_ts: int | None = None
        if start_date:
            try:
                dt = datetime.fromisoformat(start_date)
                start_ts = int(dt.timestamp())
            except ValueError:
                return ToolResult(
                    content=f"Invalid start_date format: {start_date}. Use ISO format.",
                    is_error=True,
                    error_kind=ToolErrorKind.VALIDATION,
                )
        if end_date:
            try:
                dt = datetime.fromisoformat(end_date)
                end_ts = int(calendar.timegm(dt.timetuple())) + 86399
            except ValueError:
                return ToolResult(
                    content=f"Invalid end_date format: {end_date}. Use ISO format.",
                    is_error=True,
                    error_kind=ToolErrorKind.VALIDATION,
                )

        try:
            photos = await service.search_photos(
                project_id=project_id or None,
                start_date=start_ts,
                end_date=end_ts,
                page=page,
            )
        except Exception as exc:
            logger.exception("CompanyCam search photos failed: %s", exc)
            return ToolResult(
                content=f"CompanyCam error: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )
        if not photos:
            return ToolResult(content="No photos found matching the criteria.")
        lines = [f"Found {len(photos)} photo(s):"]
        for p in photos[:20]:
            url = get_photo_url(p)
            desc = f" - {p.description}" if p.description else ""
            lines.append(f"- ID: {p.id}{desc}: {url}")
        if len(photos) > 20:
            lines.append(f"(Showing 20 of {len(photos)})")
        if len(photos) >= 50:
            lines.append(f"(Page {page}. More results may be available on the next page.)")
        return ToolResult(content="\n".join(lines))

    # ------------------------------------------------------------------
    # New tools: checklists
    # ------------------------------------------------------------------

    async def companycam_list_checklists(project_id: str) -> ToolResult:
        """List checklists for a CompanyCam project."""
        try:
            checklists = await service.list_project_checklists(project_id)
        except Exception as exc:
            logger.exception("CompanyCam list checklists failed: %s", exc)
            return ToolResult(
                content=f"CompanyCam error: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )
        if not checklists:
            return ToolResult(content="No checklists found on this project.")
        lines = [f"Found {len(checklists)} checklist(s):"]
        for cl in checklists:
            status = "completed" if cl.completed_at else "in progress"
            lines.append(f"- {cl.name or 'Untitled'} (ID: {cl.id}) [{status}]")
        return ToolResult(content="\n".join(lines))

    async def companycam_get_checklist(
        project_id: str,
        checklist_id: str,
    ) -> ToolResult:
        """Get detailed checklist with tasks and completion status."""
        try:
            cl = await service.get_checklist(project_id, checklist_id)
        except Exception as exc:
            logger.exception("CompanyCam get checklist failed: %s", exc)
            return ToolResult(
                content=f"CompanyCam error: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )
        status = "completed" if cl.completed_at else "in progress"
        lines = [f"Checklist: {cl.name or 'Untitled'} (ID: {cl.id}) [{status}]"]
        all_tasks = list(cl.sectionless_tasks or [])
        for section in cl.sections or []:
            lines.append(f"\n## {section.title or 'Untitled Section'}")
            for task in section.tasks or []:
                done = "[x]" if task.completed_at else "[ ]"
                lines.append(f"  {done} {task.title or 'Untitled'}")
                all_tasks.append(task)
        if cl.sectionless_tasks:
            for task in cl.sectionless_tasks:
                done = "[x]" if task.completed_at else "[ ]"
                lines.append(f"  {done} {task.title or 'Untitled'}")
        total = len(all_tasks)
        completed = sum(1 for t in all_tasks if t.completed_at)
        lines.append(f"\nProgress: {completed}/{total} tasks completed")
        return ToolResult(content="\n".join(lines))

    async def companycam_create_checklist(
        project_id: str,
        template_id: str,
    ) -> ToolResult:
        """Create a checklist on a project from a template."""
        try:
            cl = await service.create_project_checklist(project_id, template_id)
        except Exception as exc:
            logger.exception("CompanyCam create checklist failed: %s", exc)
            return ToolResult(
                content=f"CompanyCam error: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )
        return ToolResult(
            content=(
                f"Created checklist '{cl.name or 'Untitled'}' (ID: {cl.id}) "
                f"on project {project_id}."
            ),
        )

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
            name=ToolName.COMPANYCAM_UPLOAD_PHOTO,
            description=(
                "Upload a photo from the conversation to a CompanyCam project. "
                "Search for the project first, then upload with tags and description."
            ),
            function=companycam_upload_photo,
            params_model=CompanyCamUploadPhotoParams,
            usage_hint=(
                "When the user sends a photo and you know the client/job context, "
                "search for the CompanyCam project, then upload the photo with "
                "relevant tags (e.g. 'kitchen', 'demo', 'before')."
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
        Tool(
            name=ToolName.COMPANYCAM_ADD_COMMENT,
            description="Add a comment to a CompanyCam project or photo",
            function=companycam_add_comment,
            params_model=CompanyCamAddCommentParams,
            usage_hint=(
                "Add a note or comment to a project (target_type='project') "
                "or a specific photo (target_type='photo')."
            ),
        ),
        Tool(
            name=ToolName.COMPANYCAM_LIST_COMMENTS,
            description="List comments on a CompanyCam project or photo",
            function=companycam_list_comments,
            params_model=CompanyCamListCommentsParams,
            usage_hint=(
                "View discussion on a project (target_type='project') "
                "or a specific photo (target_type='photo')."
            ),
        ),
        Tool(
            name=ToolName.COMPANYCAM_TAG_PHOTO,
            description="Add tags to a CompanyCam photo for organization",
            function=companycam_tag_photo,
            params_model=CompanyCamTagPhotoParams,
            usage_hint="Tag photos with descriptive labels like 'before', 'kitchen', 'damage'.",
        ),
        Tool(
            name=ToolName.COMPANYCAM_DELETE_PHOTO,
            description=("WARNING: Permanently delete a CompanyCam photo. This cannot be undone."),
            function=companycam_delete_photo,
            params_model=CompanyCamDeletePhotoParams,
            usage_hint="Only delete a photo if the user explicitly asks.",
        ),
        Tool(
            name=ToolName.COMPANYCAM_SEARCH_PHOTOS,
            description="Search photos across all CompanyCam projects",
            function=companycam_search_photos,
            params_model=CompanyCamSearchPhotosParams,
            usage_hint="Find photos by project, date range, or browse recent photos.",
        ),
        Tool(
            name=ToolName.COMPANYCAM_LIST_CHECKLISTS,
            description="List checklists for a CompanyCam project",
            function=companycam_list_checklists,
            params_model=CompanyCamListChecklistsParams,
            usage_hint="Check what checklists exist on a project and their status.",
        ),
        Tool(
            name=ToolName.COMPANYCAM_GET_CHECKLIST,
            description="Get checklist details with tasks and completion status",
            function=companycam_get_checklist,
            params_model=CompanyCamGetChecklistParams,
            usage_hint="View full checklist details including all tasks and progress.",
        ),
        Tool(
            name=ToolName.COMPANYCAM_CREATE_CHECKLIST,
            description="Create a checklist on a CompanyCam project from a template",
            function=companycam_create_checklist,
            params_model=CompanyCamCreateChecklistParams,
            usage_hint=(
                "Create a new checklist from a template. "
                "Use list_checklists or ask the user which template to use."
            ),
        ),
    ]


# ---------------------------------------------------------------------------
# Connect tool (separate, always available when integration is registered)
# ---------------------------------------------------------------------------


def _create_connect_tool(user_id: str) -> Tool:
    """Create the companycam_connect tool for storing an API token."""

    async def companycam_connect(api_token: str) -> ToolResult:
        """Validate and store a CompanyCam API token for this user."""
        service = CompanyCamService(access_token=api_token)
        try:
            user_info = await service.validate_token()
        except Exception as exc:
            logger.warning("CompanyCam token validation failed: %s", exc)
            return ToolResult(
                content=(
                    "That token didn't work. CompanyCam returned an error. "
                    "Double-check the token and try again."
                ),
                is_error=True,
                error_kind=ToolErrorKind.AUTH,
            )

        token_data = OAuthTokenData(
            access_token=api_token,
            token_type="Bearer",
        )
        oauth_service.save_token(user_id, _INTEGRATION, token_data)

        display_name = user_info.first_name or ""
        if display_name:
            display_name = f" ({display_name})"
        logger.info("CompanyCam connected for user %s", user_id)
        return ToolResult(
            content=(
                f"CompanyCam connected successfully{display_name}. "
                "You can now search projects, upload photos, and create new projects. "
                "The connection will be active starting with your next message."
            ),
        )

    return Tool(
        name=ToolName.COMPANYCAM_CONNECT,
        description=(
            "Connect to CompanyCam by providing an API access token. "
            "The user can generate a token at app.companycam.com/access_tokens."
        ),
        function=companycam_connect,
        params_model=CompanyCamConnectParams,
        usage_hint=(
            "When the user wants to connect CompanyCam, ask for their API token. "
            "They can generate one at app.companycam.com/access_tokens. "
            "Call this tool with the token to validate and store it."
        ),
    )


# ---------------------------------------------------------------------------
# Auth check, factory, and registration
# ---------------------------------------------------------------------------


def _companycam_auth_check(ctx: ToolContext) -> str | None:
    """Check whether CompanyCam is available for this user.

    Returns None when connected (tools are available).
    Returns a reason string when not connected (tells the agent how to help).
    Checks per-user token first, then server-level env var.
    """
    from backend.app.config import settings

    token = oauth_service.load_token(ctx.user.id, _INTEGRATION)
    if token and token.access_token:
        return None
    if settings.companycam_access_token:
        return None
    return (
        "CompanyCam is not connected. "
        "Ask the user for their CompanyCam API token "
        "(they can generate one at app.companycam.com/access_tokens), "
        "then call companycam_connect with the token."
    )


async def _companycam_factory(ctx: ToolContext) -> list[Tool]:
    """Factory for CompanyCam tools."""
    tools: list[Tool] = []

    service = _load_service(ctx.user.id)
    if service is not None:
        tools.extend(_create_companycam_tools(service, ctx))

    # The connect tool is always available so users can connect
    tools.append(_create_connect_tool(ctx.user.id))

    return tools


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
                ToolName.COMPANYCAM_CONNECT,
                "Connect to CompanyCam with an API token",
                default_permission="ask",
            ),
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
