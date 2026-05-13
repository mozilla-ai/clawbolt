"""File cataloging tools for the agent."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from backend.app.agent import media_staging
from backend.app.agent.approval import ApprovalPolicy, PermissionLevel
from backend.app.agent.dto import slugify as _store_slugify
from backend.app.agent.saved_media import find_saved_file, read_saved_file_bytes
from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolReceipt, ToolResult
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

# Default landing folder when the caller does not specify one. The agent is
# expected to organize from here based on prompt guidance, but the upload
# itself must always succeed even when the conversation has no client
# context yet.
DEFAULT_INBOX_FOLDER = "/Inbox"

# Allowed in any single path segment. Permissive enough to let the agent
# express "Acme - 123 Main St" or "Job (final)" while still rejecting path
# traversal and shell metacharacters.
_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9 _.,()&'+-]+$")

# Hard caps. A real-world client folder is ~30-60 chars; nothing should need
# more, and a bounded validator beats letting the LLM invent 4 KB paths.
MAX_PATH_LENGTH = 256
MAX_PATH_DEPTH = 6


class UploadToStorageParams(BaseModel):
    """Parameters for the upload_to_storage tool."""

    folder_path: str | None = Field(
        default=None,
        description=(
            "Destination folder, leading slash required (e.g. '/Inbox', "
            "'/Acme - 123 Main/photos'). Defaults to /Inbox when omitted."
        ),
    )
    description: str = Field(
        default="",
        description=(
            "Short human-readable description of the file. Used as the "
            "Drive description field and as the filename slug."
        ),
    )
    original_url: str | None = Field(
        default=None,
        description=(
            "Original URL or media handle (e.g. 'media_ab12cd') of the file "
            "to upload. When omitted, the tool uses the only file attached "
            "to the current message, or the most recently staged file."
        ),
    )
    mime_type: str = Field(
        default="image/jpeg",
        description="MIME type of the file (default: image/jpeg)",
    )


class MoveFileParams(BaseModel):
    """Parameters for the move_file tool."""

    from_path: str = Field(
        description=(
            "Current storage path of the file, as quoted by find_saved_files"
            " (e.g. /Inbox/photo_001.jpg)"
        ),
    )
    to_folder: str = Field(
        description=(
            "Destination folder, leading slash required (e.g. '/Acme - 123 Main/photos')."
        ),
    )
    new_filename: str | None = Field(
        default=None,
        description=(
            "Optional new filename. When omitted the original filename is"
            " kept (with a numeric suffix if a name collision occurs)."
        ),
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


def _normalize_folder_path(raw: str | None) -> tuple[str | None, str | None]:
    """Validate and normalize a caller-supplied folder path.

    Returns ``(normalized, None)`` on success or ``(None, error_message)``
    on failure. Normalization collapses trailing slashes and lower-bound
    bare ``/`` to root (which the storage backend treats as the user's
    top-level Clawbolt folder). Validation rejects path traversal,
    backslashes, and segments outside :data:`_PATH_SEGMENT_RE`.

    Defensive against the LLM passing odd values: empty string, missing
    leading slash, ``..`` traversal, backslashes, control characters,
    overly long or deeply nested paths.
    """
    if raw is None or not raw.strip():
        return DEFAULT_INBOX_FOLDER, None

    path = raw.strip()
    if not path.startswith("/"):
        return None, "folder_path must start with '/' (e.g. '/Inbox')."
    if len(path) > MAX_PATH_LENGTH:
        return None, f"folder_path is too long (max {MAX_PATH_LENGTH} characters)."
    if "\\" in path:
        return None, "folder_path must use forward slashes only."

    # Strip trailing slash so '/Foo/' and '/Foo' resolve identically. Root
    # stays as a single '/' so the upload backend can recognize it.
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    if path == "":
        path = "/"

    if path == "/":
        return path, None

    segments = path[1:].split("/")
    if len(segments) > MAX_PATH_DEPTH:
        return None, f"folder_path nests too deeply (max {MAX_PATH_DEPTH} levels)."
    for seg in segments:
        if not seg:
            return None, "folder_path must not contain empty segments."
        if seg in (".", ".."):
            return None, "folder_path must not contain '.' or '..'."
        if not _PATH_SEGMENT_RE.match(seg):
            return None, f"folder_path segment {seg!r} contains unsupported characters."

    return path, None


def _build_filename(
    description: str | None,
    index: int = 1,
    extension: str = "jpg",
) -> str:
    """Build a meaningful filename from description or fallback."""
    if description and description.strip():
        base = _store_slugify(description, max_length=FILENAME_SLUG_MAX_LENGTH)
    else:
        base = "file"

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
        folder_path: str | None = None,
        description: str = "",
        original_url: str | None = None,
        mime_type: str = "image/jpeg",
    ) -> ToolResult:
        """Upload a file to the user's cloud storage."""
        normalized_folder, path_error = _normalize_folder_path(folder_path)
        if normalized_folder is None:
            return ToolResult(
                content=path_error or "Invalid folder_path.",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )

        # Resolve media handles: the LLM may pass a handle from
        # analyze_photo instead of the actual URL.
        if original_url and original_url not in media_map:
            resolved = await media_staging.resolve_media_ref(user.id, original_url)
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
        elif not original_url:
            # Cross-turn fallback: the agent called upload_to_storage on a
            # turn with no attachments, so neither ``media_map`` nor
            # ``resolve_media_ref`` had anything to chew on. Reach into
            # staging directly and pick the most recently staged photo.
            # ``_file_factory`` deliberately does NOT pre-load these into
            # ``media_map`` because that would mean reading every staged
            # photo off disk on every agent turn, just in case.
            staged_urls = await media_staging.list_urls_for_user(user.id)
            if staged_urls:
                first_staged = staged_urls[0]
                resolved = await media_staging.resolve_media_ref(user.id, first_staged)
                if resolved is not None:
                    _, file_bytes, mime_type = resolved
                    original_url = first_staged

        # The download layer knows the real mime type; prefer that over the
        # LLM-supplied argument so PDFs or HEICs don't get mislabeled.
        if original_url:
            staged_mime = await media_staging.get_mime_type(user.id, original_url)
            if staged_mime:
                mime_type = staged_mime

        if not file_bytes:
            # A prior tool call in this turn already shipped this handle to
            # storage and ``evict``ed the bytes. Surface the prior receipt
            # so the model doesn't read this as a failure and retry. Mirrors
            # the CompanyCam idempotency in ``companycam_upload_photo``.
            if original_url:
                prior = media_staging.get_uploaded(user.id, original_url)
                if prior is not None and prior.service == "storage":
                    logger.warning(
                        "media_handle_referenced_after_eviction service=storage "
                        "user=%s handle=%s prior_path=%s",
                        user.id,
                        original_url,
                        prior.external_id,
                    )
                    return ToolResult(
                        content=(
                            f"File {original_url} was already uploaded earlier in "
                            f"this turn to {prior.external_id}. Not re-uploading."
                        ),
                        receipt=ToolReceipt(
                            action="File already in Drive",
                            target=prior.target or prior.external_id,
                            url=prior.url or None,
                        ),
                    )
            logger.warning("upload_to_storage called but no file content available")
            return ToolResult(
                content=(
                    "No file content available to upload. This tool only works with "
                    "media attached to the current message or still in the staging "
                    "cache. To move a previously saved file, use move_file with the "
                    "storage path from find_saved_files."
                ),
                is_error=True,
                error_kind=ToolErrorKind.NOT_FOUND,
            )

        logger.info(
            "Cataloging file: folder=%s, mime=%s, size=%d bytes",
            normalized_folder,
            mime_type,
            len(file_bytes),
        )

        # Pick the next sequence number from the destination folder.
        extension = _extension_from_mime(mime_type)
        await storage.create_folder(normalized_folder)
        existing = await storage.list_folder(normalized_folder)
        filename = _build_filename(description, index=len(existing) + 1, extension=extension)

        saved = await storage.upload_file(
            file_bytes,
            normalized_folder,
            filename,
            mime_type=mime_type,
            description=description,
        )

        if original_url:
            # Record the receipt before evicting so a same-turn retry on the
            # same handle hits an idempotent result instead of NOT_FOUND.
            media_staging.mark_uploaded(
                user.id,
                original_url,
                service="storage",
                external_id=saved.path,
                url=saved.web_view_link or "",
                target=filename,
                status="uploaded",
            )
            await media_staging.evict(user.id, original_url)

        logger.info("File cataloged: %s", saved.path)
        return ToolResult(
            content=f"Uploaded {filename} to {normalized_folder} ({saved.path})",
            receipt=ToolReceipt(
                action="Uploaded file to Drive",
                target=saved.path,
                url=saved.web_view_link or None,
            ),
        )

    async def move_file(
        from_path: str,
        to_folder: str,
        new_filename: str | None = None,
    ) -> ToolResult:
        """Move a saved file to a new folder, optionally renaming it."""
        current_path = from_path.strip()
        if not current_path.startswith("/"):
            current_path = f"/{current_path}"

        normalized_folder, path_error = _normalize_folder_path(to_folder)
        if normalized_folder is None:
            return ToolResult(
                content=path_error or "Invalid to_folder.",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )

        parts = current_path.rsplit("/", 1)
        if len(parts) != 2 or not parts[1]:
            return ToolResult(
                content=f"Cannot parse storage path: {current_path}",
                is_error=True,
                error_kind=ToolErrorKind.INTERNAL,
            )
        old_folder, old_filename = parts

        if old_folder == "":
            old_folder = "/"

        existing = await find_saved_file(storage, current_path)
        if existing is None:
            return ToolResult(
                content=f"File not found at {current_path}",
                is_error=True,
                error_kind=ToolErrorKind.NOT_FOUND,
            )

        # Pick the target filename: caller override if provided, otherwise
        # keep the original. Add a numeric suffix if there is already a
        # file of that name in the destination so the move never silently
        # overwrites.
        await storage.create_folder(normalized_folder)
        target_existing = await storage.list_folder(normalized_folder)
        existing_names = {f.name for f in target_existing}
        if new_filename and new_filename.strip():
            target_filename = new_filename.strip()
        else:
            target_filename = old_filename
        if target_filename in existing_names:
            stem, dot, ext = target_filename.rpartition(".")
            base = stem if dot else target_filename
            ext_suffix = f".{ext}" if dot else ""
            n = 2
            while f"{base}_{n:03d}{ext_suffix}" in existing_names:
                n += 1
            target_filename = f"{base}_{n:03d}{ext_suffix}"

        try:
            moved = await storage.move_file(
                old_folder, old_filename, normalized_folder, target_filename
            )
        except FileNotFoundError:
            return ToolResult(
                content=f"File not found at {current_path}",
                is_error=True,
                error_kind=ToolErrorKind.NOT_FOUND,
            )

        logger.info("File moved: %s -> %s", current_path, moved.path)
        return ToolResult(
            content=f"Moved {old_filename} to {moved.path}",
            receipt=ToolReceipt(
                action="Moved file in Drive",
                target=moved.path,
                url=moved.web_view_link or None,
            ),
        )

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
                "storage. The caller picks the destination folder via "
                "folder_path; when omitted the file lands in /Inbox. The "
                "result includes a share link the user can tap. To move a "
                "file that was already saved on a prior turn, use move_file."
            ),
            function=upload_to_storage,
            params_model=UploadToStorageParams,
            usage_hint="Save a recently received file to the user's Drive and return the link.",
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                description_builder=lambda args: (
                    f"Upload file to {args.get('folder_path') or '/Inbox'}"
                ),
            ),
        ),
        Tool(
            name=ToolName.MOVE_FILE,
            description=(
                "Move a previously saved file to a new folder, optionally "
                "renaming it. Use this when the user later supplies the "
                "context that decides where a file should live (a client "
                "folder, a topic-specific folder, etc.). Quote from_path "
                "from find_saved_files (for example /Inbox/photo_001.jpg) "
                "and pass to_folder with a leading slash."
            ),
            function=move_file,
            params_model=MoveFileParams,
            usage_hint="Move a saved file to a different folder in the user's Drive.",
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                description_builder=lambda args: (
                    f"Move file to {args.get('to_folder') or 'a new folder'}"
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
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                description_builder=lambda args: (
                    f"Search saved files for '{args['query']}'"
                    if args.get("query")
                    else "List recent saved files"
                ),
            ),
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
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                description_builder=lambda args: f"Analyze saved file {args['file_ref']}",
            ),
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
    # Don't eagerly load every staged photo here -- that would read all
    # of the user's 7-day window off disk on every agent turn just in
    # case ``upload_to_storage`` ends up firing. ``upload_to_storage``
    # has its own fallback path that reaches into staging only when the
    # LLM actually calls it without an ``original_url``.
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
                ToolName.MOVE_FILE,
                "Move files between folders in Google Drive",
                default_permission="ask",
            ),
            SubToolInfo(
                ToolName.FIND_SAVED_FILES,
                "Find previously saved files in Drive",
                default_permission="ask",
            ),
            SubToolInfo(
                ToolName.ANALYZE_SAVED_FILE,
                "Analyze a previously saved image",
                default_permission="ask",
            ),
        ],
        auth_check=_file_auth_check,
    )


_register()
