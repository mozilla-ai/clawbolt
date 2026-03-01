"""File cataloging tools for the agent."""

import datetime
import logging
import re

from sqlalchemy.orm import Session

from backend.app.agent.tools.base import Tool
from backend.app.media.download import MIME_EXTENSIONS
from backend.app.models import Contractor, MediaFile
from backend.app.services.storage_service import StorageBackend

logger = logging.getLogger(__name__)

DESCRIPTION_SLUG_MAX_LENGTH = 40
FILENAME_SLUG_MAX_LENGTH = 30

# Category to folder mapping
CATEGORY_FOLDERS: dict[str, str] = {
    "job_photo": "Job Photos",
    "estimate": "Estimates",
    "document": "Documents",
    "voice_note": "Voice Notes",
}


def _slugify(text: str, max_length: int = DESCRIPTION_SLUG_MAX_LENGTH) -> str:
    """Convert text to a filesystem-safe slug."""
    slug = text.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "_", slug)
    return slug[:max_length].rstrip("_")


def _build_folder_path(
    category: str,
    job_name: str | None = None,
) -> str:
    """Build the folder path for a file upload."""
    today = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    folder = CATEGORY_FOLDERS.get(category, "Other")

    if job_name:
        return f"/{folder}/{today}_{_slugify(job_name)}"
    return f"/{folder}/{today}"


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
        "voice_note": "voice_note",
    }
    base = fallback_names.get(category, "file")

    if description and description.strip():
        base = _slugify(description, max_length=FILENAME_SLUG_MAX_LENGTH)

    return f"{base}_{index:03d}.{extension}"


def _extension_from_mime(mime_type: str) -> str:
    """Get file extension from MIME type."""
    dotted = MIME_EXTENSIONS.get(mime_type, ".bin")
    return dotted.lstrip(".")


def create_file_tools(
    db: Session,
    contractor: Contractor,
    storage: StorageBackend,
    pending_media: dict[str, bytes] | None = None,
) -> list[Tool]:
    """Create file cataloging tools for the agent.

    Args:
        db: Database session
        contractor: The contractor
        storage: Storage backend (Dropbox, Google Drive, or mock)
        pending_media: Dict of original_url -> file bytes for media in the current message
    """
    media_map = pending_media or {}

    async def upload_to_storage(
        file_category: str,
        description: str = "",
        job_name: str | None = None,
        original_url: str | None = None,
        mime_type: str = "image/jpeg",
    ) -> str:
        """Upload a file to the contractor's cloud storage."""
        # Determine file content
        file_bytes = b""
        if original_url and original_url in media_map:
            file_bytes = media_map[original_url]
        elif media_map:
            # Use the first available media if no specific URL provided
            first_url = next(iter(media_map))
            file_bytes = media_map[first_url]
            original_url = original_url or first_url

        if not file_bytes:
            return "No file content available to upload."

        # Build path and filename
        folder_path = _build_folder_path(file_category, job_name)
        extension = _extension_from_mime(mime_type)

        # Count existing files to get index
        existing = (
            db.query(MediaFile)
            .filter(
                MediaFile.contractor_id == contractor.id,
                MediaFile.storage_path.like(f"{folder_path}%"),
            )
            .count()
        )

        filename = _build_filename(
            description, file_category, index=existing + 1, extension=extension
        )

        # Create folder and upload
        await storage.create_folder(folder_path)
        storage_url = await storage.upload_file(file_bytes, folder_path, filename)

        # Create MediaFile record
        media_file = MediaFile(
            contractor_id=contractor.id,
            original_url=original_url or "",
            mime_type=mime_type,
            processed_text=description,
            storage_url=storage_url,
            storage_path=f"{folder_path}/{filename}",
        )
        db.add(media_file)
        db.commit()

        return f"Uploaded {filename} to {folder_path}/ ({storage_url})"

    return [
        Tool(
            name="upload_to_storage",
            description=(
                "Upload a file to the contractor's cloud storage (Dropbox or Google Drive). "
                "Files are organized into folders by category. Use when the contractor sends "
                "photos, documents, or files that should be saved."
            ),
            function=upload_to_storage,
            parameters={
                "type": "object",
                "properties": {
                    "file_category": {
                        "type": "string",
                        "enum": ["job_photo", "estimate", "document", "voice_note"],
                        "description": "Category for organizing the file",
                    },
                    "description": {
                        "type": "string",
                        "description": "Brief description for the filename (from vision analysis)",
                    },
                    "job_name": {
                        "type": "string",
                        "description": "Name of the job/project this file relates to (optional)",
                    },
                    "original_url": {
                        "type": "string",
                        "description": "Original URL of the media to upload (optional)",
                    },
                    "mime_type": {
                        "type": "string",
                        "description": "MIME type of the file (default: image/jpeg)",
                    },
                },
                "required": ["file_category"],
            },
        ),
    ]
