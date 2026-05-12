"""File cataloging tools for the agent."""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from backend.app.agent import media_staging
from backend.app.agent.approval import ApprovalPolicy, PermissionLevel
from backend.app.agent.dto import slugify as _store_slugify
from backend.app.agent.saved_media import find_saved_file, read_saved_file_bytes
from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.media.download import MIME_EXTENSIONS
from backend.app.media.pipeline import run_vision_on_media
from backend.app.models import User
from backend.app.services.storage_service import SavedFile, StorageBackend

if TYPE_CHECKING:
    from backend.app.agent.tools.registry import ToolContext

logger = logging.getLogger(__name__)

DESCRIPTION_SLUG_MAX_LENGTH = 40
FILENAME_SLUG_MAX_LENGTH = 30

# Category to subfolder mapping (under client folders)
CATEGORY_SUBFOLDERS: dict[str, str] = {
    "job_photo": "photos",
    "estimate": "estimates",
    "invoice": "invoices",
    "document": "documents",
}

FileCategory = Literal["job_photo", "estimate", "document"]


class UploadToStorageParams(BaseModel):
    """Parameters for the upload_to_storage tool."""

    file_category: FileCategory = Field(
        description="Category for organizing the file",
    )
    description: str = Field(
        default="",
        description="Brief description for the filename",
    )
    client_name: str | None = Field(
        default=None,
        description="Client name for folder organization",
    )
    client_address: str | None = Field(
        default=None,
        description="Client or job address for folder organization",
    )
    original_url: str | None = Field(
        default=None,
        description="Original URL of the media to upload",
    )
    mime_type: str = Field(
        default="image/jpeg",
        description="MIME type of the file (default: image/jpeg)",
    )


class OrganizeFileParams(BaseModel):
    """Parameters for the organize_file tool."""

    storage_path: str = Field(
        description=(
            "Current storage path of the file to move, as quoted by find_saved_files"
            " (e.g. /Unsorted/2026-04-16/file_001.jpg)"
        ),
    )
    file_category: FileCategory = Field(
        description="Category for organizing the file",
    )
    client_name: str | None = Field(
        default=None,
        description="Client name for folder organization",
    )
    client_address: str | None = Field(
        default=None,
        description="Client or job address for folder organization",
    )
    description: str = Field(
        default="",
        description="Brief description for the filename",
    )


class FindSavedFilesParams(BaseModel):
    """Parameters for the find_saved_files tool."""

    query: str = Field(
        default="",
        description=(
            "Short text to match against filenames or saved descriptions. "
            "Leave empty to list the most recent saved files."
        ),
    )
    limit: int = Field(
        default=5,
        ge=1,
        le=10,
        description="Maximum number of saved files to return.",
    )


class AnalyzeSavedFileParams(BaseModel):
    """Parameters for the analyze_saved_file tool."""

    file_ref: str = Field(
        description=(
            "Saved file reference from find_saved_files, normally a storage path"
            " like /Astro Home/photos/foo.jpg"
        ),
    )
    context: str = Field(
        default="",
        description="Optional short context to guide the analysis.",
    )


def _build_client_folder(
    client_name: str | None = None,
    client_address: str | None = None,
) -> str:
    """Build a top-level client folder name from available context.

    Returns a combined folder name like "John Smith - 116 Virginia Ave",
    or an empty string when no context is available.
    """
    parts: list[str] = []
    if client_name and client_name.strip():
        parts.append(client_name.strip())
    if client_address and client_address.strip():
        parts.append(client_address.strip())
    return " - ".join(parts)


def build_folder_path(
    category: str,
    client_name: str | None = None,
    client_address: str | None = None,
) -> str:
    """Build the folder path for a file upload.

    When client context is available, organizes by client:
        /{Client Name - Address}/{category_subfolder}
    When no client context, falls back to date-based:
        /Unsorted/{date}
    """
    client_folder = _build_client_folder(client_name, client_address)

    if client_folder:
        subfolder = CATEGORY_SUBFOLDERS.get(category, "other")
        return f"/{client_folder}/{subfolder}"

    today = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    return f"/Unsorted/{today}"


def _build_filename(
    description: str | None,
    category: str,
    index: int = 1,
    extension: str = "jpg",
) -> str:
    """Build a meaningful filename from description or fallback."""
    fallback_names: dict[str, str] = {
        "job_photo": "photo",
        "estimate": "estimate",
        "document": "document",
    }
    base = fallback_names.get(category, "file")

    if description and description.strip():
        base = _store_slugify(description, max_length=FILENAME_SLUG_MAX_LENGTH)

    return f"{base}_{index:03d}.{extension}"


def _extension_from_mime(mime_type: str) -> str:
    """Get file extension from MIME type."""
    dotted = MIME_EXTENSIONS.get(mime_type, ".bin")
    return dotted.lstrip(".")


def _format_saved_file(saved: SavedFile) -> str:
    """Render one saved file as a compact, parseable line for the LLM."""
    parts = [f"path={saved.path}"]
    if saved.mime_type and saved.mime_type != "image/jpeg":
        parts.append(f"mime={saved.mime_type}")
    if saved.description:
        parts.append(f"description={saved.description}")
    if saved.modified_at:
        parts.append(f"saved_at={saved.modified_at}")
    if saved.web_view_link:
        parts.append(f"url={saved.web_view_link}")
    return "- " + " | ".join(parts)


def create_file_tools(
    user: User,
    storage: StorageBackend,
    pending_media: dict[str, bytes] | None = None,
    turn_text: str = "",
) -> list[Tool]:
    """Create file cataloging tools for the agent.

    Args:
        user: The user
        storage: Storage backend (Google Drive or mock)
        pending_media: Dict of original_url -> file bytes available for upload.
            Includes bytes from the current message and any recent staged
            media bytes from prior turns (populated by ``_file_factory``).
        turn_text: Current turn text, used as fallback analysis context.
    """
    media_map = pending_media or {}
    # Per-turn cache: same closure lifetime as the tool list, so a saved file
    # analyzed twice in one turn only pays the vision cost once.
    saved_analysis_cache: dict[str, str] = {}

    async def upload_to_storage(
        file_category: str,
        description: str = "",
        client_name: str | None = None,
        client_address: str | None = None,
        original_url: str | None = None,
        mime_type: str = "image/jpeg",
    ) -> ToolResult:
        """Upload a file to the user's cloud storage."""
        # Resolve media handles: the LLM may pass a handle from
        # analyze_photo instead of the actual URL.
        if original_url and original_url not in media_map:
            resolved = media_staging.resolve_media_ref(user.id, original_url)
            if resolved is not None:
                resolved_url, resolved_bytes, resolved_mime = resolved
                original_url = resolved_url
                media_map.setdefault(resolved_url, resolved_bytes)
                mime_type = resolved_mime

        # Determine file content
        file_bytes = b""
        if original_url and original_url in media_map:
            file_bytes = media_map[original_url]
        elif media_map:
            # Use the first available media if no specific URL provided
            first_url = next(iter(media_map))
            file_bytes = media_map[first_url]
            original_url = original_url or first_url

        # The download layer knows the real mime type; prefer that over the
        # LLM-supplied argument so PDFs or HEICs don't get mislabeled.
        if original_url:
            staged_mime = media_staging.get_mime_type(user.id, original_url)
            if staged_mime:
                mime_type = staged_mime

        if not file_bytes:
            logger.warning("upload_to_storage called but no file content available")
            return ToolResult(
                content=(
                    "No file content available to upload. This tool only works with "
                    "media attached to the current message. To organize a previously "
                    "received file, use the organize_file tool with the storage path "
                    "from find_saved_files."
                ),
                is_error=True,
                error_kind=ToolErrorKind.NOT_FOUND,
            )

        logger.info(
            "Cataloging file: category=%s, mime=%s, size=%d bytes",
            file_category,
            mime_type,
            len(file_bytes),
        )

        # Build path and pick the next sequence number from the destination folder.
        folder_path = build_folder_path(file_category, client_name, client_address)
        extension = _extension_from_mime(mime_type)
        await storage.create_folder(folder_path)
        existing = await storage.list_folder(folder_path)
        filename = _build_filename(
            description, file_category, index=len(existing) + 1, extension=extension
        )

        saved = await storage.upload_file(
            file_bytes,
            folder_path,
            filename,
            mime_type=mime_type,
            description=description,
        )

        if original_url:
            media_staging.evict(user.id, original_url)

        logger.info("File cataloged: %s", saved.path)
        return ToolResult(content=f"Uploaded {filename} to {folder_path}/ ({saved.path})")

    async def organize_file(
        storage_path: str,
        file_category: str,
        client_name: str | None = None,
        client_address: str | None = None,
        description: str = "",
    ) -> ToolResult:
        """Move an auto-saved file from Unsorted into the correct client folder."""
        current_path = storage_path.strip()
        if not current_path.startswith("/"):
            current_path = f"/{current_path}"

        new_folder = build_folder_path(file_category, client_name, client_address)

        # Guard: without client context the file would just move within Unsorted
        if new_folder.startswith("/Unsorted"):
            return ToolResult(
                content=(
                    "Error: client_name or client_address is required to organize a file. "
                    "Please provide at least one so the file can be moved to a client folder."
                ),
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )

        # Check if already in a client folder (not Unsorted)
        if not current_path.startswith("/Unsorted/"):
            return ToolResult(content=f"File is already organized at {current_path}")

        parts = current_path.rsplit("/", 1)
        if len(parts) != 2 or not parts[1]:
            return ToolResult(
                content=f"Cannot parse storage path: {current_path}",
                is_error=True,
                error_kind=ToolErrorKind.INTERNAL,
            )
        old_folder, old_filename = parts

        existing = await find_saved_file(storage, current_path)
        if existing is None:
            return ToolResult(
                content=f"File not found at {current_path}",
                is_error=True,
                error_kind=ToolErrorKind.NOT_FOUND,
            )

        extension = old_filename.rsplit(".", 1)[-1] if "." in old_filename else "bin"
        await storage.create_folder(new_folder)
        target_existing = await storage.list_folder(new_folder)
        new_filename = _build_filename(
            description, file_category, index=len(target_existing) + 1, extension=extension
        )

        try:
            moved = await storage.move_file(old_folder, old_filename, new_folder, new_filename)
        except FileNotFoundError:
            return ToolResult(
                content=f"File not found at {current_path}",
                is_error=True,
                error_kind=ToolErrorKind.NOT_FOUND,
            )

        logger.info("File organized: %s -> %s", current_path, moved.path)
        return ToolResult(content=f"Moved {old_filename} to {moved.path}")

    async def find_saved_files(query: str = "", limit: int = 5) -> ToolResult:
        """Search previously saved files in durable storage."""
        matches = await storage.search_files(query=query, limit=limit)
        if not matches:
            if query.strip():
                content = f'No saved files matched "{query}".'
            else:
                content = "No saved files found."
            return ToolResult(
                content=content,
                is_error=True,
                error_kind=ToolErrorKind.NOT_FOUND,
            )

        heading = "Recent saved files:" if not query.strip() else f'Saved files matching "{query}":'
        lines = [_format_saved_file(saved) for saved in matches]
        return ToolResult(content=heading + "\n" + "\n".join(lines))

    async def analyze_saved_file(file_ref: str, context: str = "") -> ToolResult:
        """Run vision analysis on an image that was already saved to storage."""
        saved = await find_saved_file(storage, file_ref)
        if saved is None:
            return ToolResult(
                content=f"Saved file not found for reference: {file_ref}",
                is_error=True,
                error_kind=ToolErrorKind.NOT_FOUND,
            )

        cache_key = saved.path or file_ref
        cached = saved_analysis_cache.get(cache_key)
        if cached is not None:
            return ToolResult(content=cached)

        mime_type = saved.mime_type or ""
        if not mime_type.startswith("image/"):
            return ToolResult(
                content=(
                    f"Saved file {saved.path!r} is {mime_type or 'unknown'}, "
                    "not an image. analyze_saved_file only works on saved photos."
                ),
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )

        try:
            content = await read_saved_file_bytes(storage, saved)
        except FileNotFoundError:
            return ToolResult(
                content=f"Saved file {saved.path!r} could not be loaded from storage.",
                is_error=True,
                error_kind=ToolErrorKind.NOT_FOUND,
            )
        except Exception as exc:
            logger.exception("Failed to load saved media %s", saved.path)
            return ToolResult(
                content=f"Couldn't load saved file {saved.path!r}: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

        effective_context = context or turn_text or "Describe this saved image."
        description = await run_vision_on_media(content, mime_type, effective_context)
        saved_analysis_cache[cache_key] = description
        logger.info(
            "analyze_saved_file ran vision for %s (chars=%d)",
            saved.path,
            len(description),
        )
        return ToolResult(content=description)

    return [
        Tool(
            name=ToolName.UPLOAD_TO_STORAGE,
            description=(
                "Upload a file attached to the current message (or a recently "
                "received one still in the staging cache) to the user's cloud "
                "storage. Files are organized by client: provide client_name or "
                "client_address to file under their folder, otherwise files go "
                "to Unsorted. If the file was already persisted to storage in a "
                "prior turn, use organize_file instead to move it."
            ),
            function=upload_to_storage,
            params_model=UploadToStorageParams,
            usage_hint="Upload a recently received file to cloud storage.",
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                description_builder=lambda args: (
                    f"Upload file to {args.get('client_name') or 'storage'}"
                ),
            ),
        ),
        Tool(
            name=ToolName.ORGANIZE_FILE,
            description=(
                "Move a previously received file from the Unsorted folder into the "
                "correct client folder. Use this when you learn which client a file "
                "belongs to. Quote the storage_path from find_saved_files (for example "
                "/Unsorted/2026-04-16/file_001.jpg) and pass at least client_name or "
                "client_address."
            ),
            function=organize_file,
            params_model=OrganizeFileParams,
            usage_hint="Move an unsorted file into the correct client folder.",
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                description_builder=lambda args: (
                    f"Move file to {args.get('client_name') or 'client'} folder"
                ),
            ),
        ),
        Tool(
            name=ToolName.FIND_SAVED_FILES,
            description=(
                "Find files that were already saved to durable storage. Use this "
                "to pull up older receipts, photos, or documents by client name, "
                "address, filename, or saved description. Only returns files "
                "Clawbolt uploaded itself; files the user added to the Clawbolt "
                "folder directly in Drive are not visible to this tool."
            ),
            function=find_saved_files,
            params_model=FindSavedFilesParams,
            usage_hint="Search durable saved files before asking the user to resend one.",
        ),
        Tool(
            name=ToolName.ANALYZE_SAVED_FILE,
            description=(
                "Run vision analysis on a previously saved image in durable storage. "
                "Quote the storage path returned by find_saved_files. Only works on images."
            ),
            function=analyze_saved_file,
            params_model=AnalyzeSavedFileParams,
            usage_hint="Inspect a saved receipt or photo again without asking for a resend.",
        ),
    ]


def _file_factory(ctx: ToolContext) -> list[Tool]:
    """Factory for file tools, used by the registry."""
    # auth_check is the user-visible gate, but defend against direct
    # invocation paths that bypass it (e.g. ``activate_specialist`` before
    # the user has connected Drive). Returning [] lets the activator log
    # "no tools produced" and skip cleanly.
    if ctx.storage is None:
        return []
    pending_media = {m.original_url: m.content for m in ctx.downloaded_media if m.content}
    # Fall back to recent staged bytes so upload_to_storage works even when the
    # agent defers the call to a later turn with no attachments of its own.
    for url, content in media_staging.get_all_for_user(ctx.user.id).items():
        pending_media.setdefault(url, content)
    return create_file_tools(ctx.user, ctx.storage, pending_media, ctx.turn_text)


async def _file_auth_check(ctx: ToolContext) -> str | None:
    """Return a "connect Drive" hint when the user hasn't authorized Drive.

    Returns ``None`` when the integration is not configured at the
    deployment level (no client id/secret), so it stays hidden rather than
    nagging users on a deployment that can't offer Drive at all.
    """
    from backend.app.config import settings
    from backend.app.services.oauth import oauth_service

    if not settings.google_drive_client_id or not settings.google_drive_client_secret:
        return None
    token = await oauth_service.load_token(ctx.user.id, "google_drive")
    if token is not None and token.access_token:
        return None
    return (
        "Google Drive is not connected. "
        "Use manage_integration(action='connect', target='google_drive') "
        "to generate a connection link for the user."
    )


def _register() -> None:
    from backend.app.agent.tools.registry import SubToolInfo, default_registry

    default_registry.register(
        "file",
        _file_factory,
        core=False,
        summary="Upload, retrieve, and organize files in the user's Google Drive",
        display_name="Google Drive",
        oauth_name="google_drive",
        dashboard_description="Upload, retrieve, and organize files in the user's Google Drive",
        # Specialist at the LLM-schema level (gated on Drive OAuth) but
        # presented as always-on in Settings: the user connects rather
        # than toggles, and a "disabled" state would be confusing.
        dashboard_always_enabled=True,
        sub_tools=[
            SubToolInfo(
                ToolName.UPLOAD_TO_STORAGE,
                "Upload files to Google Drive",
                default_permission="ask",
            ),
            SubToolInfo(
                ToolName.ORGANIZE_FILE, "Move files into client folders", default_permission="ask"
            ),
            SubToolInfo(
                ToolName.FIND_SAVED_FILES,
                "Find previously saved files in Drive",
                default_permission="always",
            ),
            SubToolInfo(
                ToolName.ANALYZE_SAVED_FILE,
                "Analyze a previously saved image",
                default_permission="always",
            ),
        ],
        auth_check=_file_auth_check,
    )


_register()
