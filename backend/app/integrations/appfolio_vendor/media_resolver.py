"""Resolve staged photos into AppFolio's base64 file payload shape.

AppFolio's notes and invoice endpoints accept JSON-inlined files via
``files: [{file_base64, name}]`` rather than multipart upload. The
agent receives photos through the OSS staging pipeline (current
message ``downloaded_media`` then the in-memory media staging cache),
and may also reference a previously saved file by its storage path.
This module hides that lookup behind one entry point so each write
tool only needs to accept a list of media references.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from backend.app.agent.tools.base import ToolErrorKind, ToolResult
from backend.app.integrations.appfolio_vendor.service import FileUpload
from backend.app.media.download import generate_filename

if TYPE_CHECKING:
    from backend.app.agent.tools.registry import ToolContext

logger = logging.getLogger(__name__)


async def resolve_staged_files(
    ctx: ToolContext,
    media_refs: list[str],
) -> list[FileUpload] | ToolResult:
    """Resolve each ``media_ref`` to bytes and return a list of FileUpload.

    Inputs are LLM-supplied references: a media handle (``media_abZtYWFs``)
    from the ``analyze_photo`` flow, an inbound message ``original_url``,
    or a Drive storage path (``/Astro Home/photos/foo.jpg``) for files
    saved in earlier turns. Each ref is checked against, in order:

    1. Media handle resolution via ``media_staging.resolve_media_ref``.
    2. The current turn's ``ctx.downloaded_media``.
    3. The ``media_staging`` cache (may have evicted older photos).
    4. The storage backend, if the ref looks like a saved file path.

    Returns a list of :class:`FileUpload` on success, or a populated
    :class:`ToolResult` with ``is_error=True`` when at least one ref
    cannot be resolved. Tools should bubble the error verbatim so the
    agent gets a deterministic "no photo found" message instead of a
    half-attached payload.

    An empty ``media_refs`` returns an empty list — tools that want a
    no-photo path should branch on the input rather than the output.
    """
    if not media_refs:
        return []

    # Lazy import keeps the test surface narrow and breaks what would
    # otherwise be a circular dependency through ``backend.app.agent``.
    from backend.app.agent import media_staging
    from backend.app.agent.saved_media import find_saved_file, read_saved_file_bytes

    staged_cache = media_staging.get_all_for_user(ctx.user.id)

    resolved: list[FileUpload] = []
    missing: list[str] = []

    for raw_ref in media_refs:
        ref = raw_ref.strip()
        if not ref:
            continue
        original_url = ref
        file_bytes: bytes = b""
        mime_type = "image/jpeg"

        # 1. Resolve a media handle to (original_url, bytes).
        handle = media_staging.resolve_media_ref(ctx.user.id, ref)
        if handle is not None:
            original_url, file_bytes, mime_type = handle

        # 2. Current message's downloaded_media.
        if not file_bytes:
            for media in ctx.downloaded_media:
                if media.original_url != original_url:
                    continue
                file_bytes = media.content
                mime_type = media.mime_type or mime_type
                break

        # 3. Media staging cache.
        if not file_bytes and original_url in staged_cache:
            file_bytes = staged_cache[original_url]

        # 4. Saved file in Drive (path-quoted by the agent).
        if not file_bytes and ctx.storage is not None:
            saved = await find_saved_file(ctx.storage, ref)
            if saved is not None:
                try:
                    file_bytes = await read_saved_file_bytes(ctx.storage, saved)
                    mime_type = saved.mime_type or mime_type
                except FileNotFoundError:
                    logger.warning("Saved file missing from storage: %s", saved.path)

        if not file_bytes:
            missing.append(ref)
            continue

        resolved.append(
            FileUpload(
                name=generate_filename(mime_type),
                data=file_bytes,
            )
        )

    if missing:
        return ToolResult(
            content=(
                "Could not resolve "
                + ", ".join(repr(m) for m in missing)
                + " to staged media. Ask the user to resend the photo(s)."
            ),
            is_error=True,
            error_kind=ToolErrorKind.NOT_FOUND,
        )
    return resolved
