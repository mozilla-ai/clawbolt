"""File cataloging tools for the agent."""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from backend.app.agent import media_staging
from backend.app.agent.approval import ApprovalPolicy, PermissionLevel
from backend.app.agent.dto import slugify as _store_slugify
from backend.app.agent.stores import MediaStore
from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.media.download import MIME_EXTENSIONS, DownloadedMedia
from backend.app.models import User
from backend.app.services.storage_service import StorageBackend

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

    original_url: str = Field(
        description="Original URL/file_id of the media to move",
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


async def auto_save_media(
    user: User,
    storage: StorageBackend,
    downloaded_media: list[DownloadedMedia],
) -> list[str]:
    """Auto-save downloaded media to storage before the agent loop.

    Persists all inbound media to /Unsorted/{date}/ immediately after
    download. Only called when the ``upload_to_storage`` permission is
    ``always``; callers must check the permission level first.

    Returns a list of storage URLs for saved files.
    """
    if not downloaded_media:
        return []

    today = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    folder_path = f"/Unsorted/{today}"
    await storage.create_folder(folder_path)

    media_store = MediaStore(user.id)
    saved_urls: list[str] = []
    for media in downloaded_media:
        extension = _extension_from_mime(media.mime_type)

        existing_count = await media_store.count_by_path_prefix(folder_path)

        filename = f"file_{existing_count + 1:03d}.{extension}"
        storage_url = await storage.upload_file(media.content, folder_path, filename)

        await media_store.create(
            original_url=media.original_url,
            mime_type=media.mime_type,
            storage_url=storage_url,
            storage_path=f"{folder_path}/{filename}",
        )
        saved_urls.append(storage_url)
        media_staging.evict(user.id, media.original_url)

    return saved_urls


def create_file_tools(
    user: User,
    storage: StorageBackend,
    pending_media: dict[str, bytes] | None = None,
) -> list[Tool]:
    """Create file cataloging tools for the agent.

    Args:
        user: The user
        storage: Storage backend (Dropbox, Google Drive, or mock)
        pending_media: Dict of original_url -> file bytes available for upload.
            Includes bytes from the current message and any recent staged
            media bytes from prior turns (populated by ``_file_factory``).
    """
    media_map = pending_media or {}

    async def upload_to_storage(
        file_category: str,
        description: str = "",
        client_name: str | None = None,
        client_address: str | None = None,
        original_url: str | None = None,
        mime_type: str = "image/jpeg",
    ) -> ToolResult:
        """Upload a file to the user's cloud storage."""
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
                    "received file, use the organize_file tool instead with the "
                    "file's original_url."
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

        # Build path and filename
        folder_path = build_folder_path(file_category, client_name, client_address)
        extension = _extension_from_mime(mime_type)

        # Count existing files to get index
        media_store = MediaStore(user.id)
        existing = await media_store.count_by_path_prefix(folder_path)

        filename = _build_filename(
            description, file_category, index=existing + 1, extension=extension
        )

        # Create folder and upload
        await storage.create_folder(folder_path)
        storage_url = await storage.upload_file(file_bytes, folder_path, filename)

        # Create media file record
        await media_store.create(
            original_url=original_url or "",
            mime_type=mime_type,
            processed_text=description,
            storage_url=storage_url,
            storage_path=f"{folder_path}/{filename}",
        )

        if original_url:
            media_staging.evict(user.id, original_url)

        logger.info("File cataloged: %s/%s -> %s", folder_path, filename, storage_url)
        return ToolResult(content=f"Uploaded {filename} to {folder_path}/ ({storage_url})")

    async def organize_file(
        original_url: str,
        file_category: str,
        client_name: str | None = None,
        client_address: str | None = None,
        description: str = "",
    ) -> ToolResult:
        """Move an auto-saved file from Unsorted into the correct client folder."""
        # Look up the media record
        media_store = MediaStore(user.id)
        media_file = await media_store.get_by_url(original_url)
        if media_file is None:
            return ToolResult(
                content=f"File not found for URL: {original_url}",
                is_error=True,
                error_kind=ToolErrorKind.NOT_FOUND,
            )

        current_path = media_file.storage_path  # e.g. /Unsorted/2026-03-02/file_001.jpg
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

        # Parse current path into folder and filename
        parts = current_path.rsplit("/", 1)
        if len(parts) != 2:
            return ToolResult(
                content=f"Cannot parse storage path: {current_path}",
                is_error=True,
                error_kind=ToolErrorKind.INTERNAL,
            )
        old_folder, old_filename = parts

        # Build new filename
        extension = old_filename.rsplit(".", 1)[-1] if "." in old_filename else "bin"
        existing = await media_store.count_by_path_prefix(new_folder)
        new_filename = _build_filename(
            description, file_category, index=existing + 1, extension=extension
        )

        # Create destination folder and move
        await storage.create_folder(new_folder)
        new_url = await storage.move_file(old_folder, old_filename, new_folder, new_filename)

        # Update the record
        update_fields: dict[str, str] = {
            "storage_path": f"{new_folder}/{new_filename}",
            "storage_url": new_url,
        }
        if description:
            update_fields["processed_text"] = description
        await media_store.update(media_file.id, **update_fields)

        media_staging.evict(user.id, original_url)

        logger.info(
            "File organized: %s -> %s/%s",
            current_path,
            new_folder,
            new_filename,
        )
        return ToolResult(content=f"Moved {old_filename} to {new_folder}/{new_filename}")

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
                "belongs to, even if the file was received in an earlier message. "
                "Requires the original_url of the file and at least a client_name "
                "or client_address to build the destination folder."
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
    ]


def _file_factory(ctx: ToolContext) -> list[Tool]:
    """Factory for file tools, used by the registry."""
    assert ctx.storage is not None
    pending_media = {m.original_url: m.content for m in ctx.downloaded_media if m.content}
    # Fall back to recent staged bytes so upload_to_storage works even when the
    # agent defers the call to a later turn with no attachments of its own.
    for url, content in media_staging.get_all_for_user(ctx.user.id).items():
        pending_media.setdefault(url, content)
    return create_file_tools(ctx.user, ctx.storage, pending_media)


def _register() -> None:
    from backend.app.agent.tools.registry import SubToolInfo, default_registry

    default_registry.register(
        "file",
        _file_factory,
        requires_storage=True,
        core=True,
        summary="Upload and organize files in cloud storage (Dropbox/Google Drive)",
        sub_tools=[
            SubToolInfo(
                ToolName.UPLOAD_TO_STORAGE,
                "Upload files to cloud storage",
                default_permission="ask",
            ),
            SubToolInfo(
                ToolName.ORGANIZE_FILE, "Move files into client folders", default_permission="ask"
            ),
        ],
    )


_register()
