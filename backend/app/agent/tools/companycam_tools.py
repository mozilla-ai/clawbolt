"""CompanyCam tools for the agent.

Provides tools to connect to CompanyCam, search/create projects, and upload
photos. The connection uses a per-user API token stored in the oauth_tokens
table (OAuth 2.0 support is planned post-MVP).
"""

from __future__ import annotations

import logging
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
        from pathlib import Path

        from backend.app.agent.stores import MediaStore
        from backend.app.config import settings
        from backend.app.routers.media_temp import create_temp_media_url

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
                        file_bytes = local_path.read_bytes()

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
        import asyncio

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
        summary=("Upload photos, search projects, and manage job documentation with CompanyCam"),
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
        ],
        auth_check=_companycam_auth_check,
    )


_register()
