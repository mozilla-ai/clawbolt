"""CompanyCam photo and comment tools.

Implements upload/tag/delete/search for photos and add/list for comments
on either projects or photos. Built by the factory in ``factory``
which passes the authenticated service and context.
"""

from __future__ import annotations

import asyncio
import calendar
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from backend.app.agent.approval import ApprovalPolicy, PermissionLevel
from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolReceipt, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.integrations.companycam.params import (
    CompanyCamAddCommentParams,
    CompanyCamDeletePhotoParams,
    CompanyCamListCommentsParams,
    CompanyCamSearchPhotosParams,
    CompanyCamTagPhotoParams,
    CompanyCamUploadPhotoParams,
)
from backend.app.integrations.companycam.receipts import (
    comment_target,
    photo_target,
    photo_url,
    project_url,
    tags_target,
)
from backend.app.integrations.companycam.service import CompanyCamService

if TYPE_CHECKING:
    from backend.app.agent.tools.registry import ToolContext

logger = logging.getLogger(__name__)


def build_photo_tools(service: CompanyCamService, ctx: ToolContext) -> list[Tool]:
    """Return the CompanyCam photo and comment Tool instances.

    Behaviour is identical to the original combined factory. The inner
    functions close over *service*, *ctx*, and the lazy-imported
    ``media_staging`` module.
    """

    from backend.app.agent import media_staging

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

        # Sanitize LLM-supplied description: the LLM sometimes passes
        # the raw vision analysis output (a dict repr or JSON object)
        # instead of a plain-text sentence. Strip it so CompanyCam
        # doesn't store garbage as the photo description.
        if description:
            description = description.strip()
            if description.startswith(("{", "[")):
                description = ""

        file_bytes: bytes = b""
        mime_type = "image/jpeg"
        photo_uri = ""

        # 0. Resolve media handles: the LLM may pass a handle
        #    (e.g. "media_abZtYWFs") from analyze_photo instead of the
        #    actual URL. Normalize to original_url + bytes before the
        #    three-tier lookup below.
        if original_url:
            resolved = media_staging.resolve_media_ref(ctx.user.id, original_url)
            if resolved is not None:
                original_url, file_bytes, mime_type = resolved

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
            elif not original_url and all_staged:
                # Only grab an arbitrary photo when the LLM did not
                # specify which one. When original_url is set but not
                # found, we must NOT silently substitute another photo.
                first_url = next(iter(all_staged))
                file_bytes = all_staged[first_url]
                original_url = first_url

        # 3. Fall back to MediaFile records (already saved to storage)
        if not file_bytes:
            media_store = MediaStore(ctx.user.id)
            media_file = None
            if original_url:
                media_file = await media_store.get_by_url(original_url)
            if media_file is None and not original_url:
                # Same guard as step 2: only grab the most recent media
                # when the LLM did not specify a particular URL.
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

        app_url = photo_url(photo.id)
        logger.info(
            "CompanyCam photo result: project=%s id=%s status=%s url=%s",
            project_id,
            photo.id,
            status,
            app_url,
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
                receipt=ToolReceipt(
                    action="Photo already in CompanyCam",
                    target=photo_target(photo),
                    url=app_url,
                ),
            )

        status_note = ""
        if status == "pending":
            status_note = " (still processing, may take a moment to appear)"
        return ToolResult(
            content=f"Photo uploaded to CompanyCam project {project_id}{status_note}.",
            receipt=ToolReceipt(
                action="Uploaded photo to CompanyCam",
                target=photo_target(photo),
                url=app_url,
            ),
        )

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
        parent_url = project_url(target_id) if target_type == "project" else photo_url(target_id)
        return ToolResult(
            content=f"Comment added to {target_type} {target_id} (ID: {comment.id}).",
            receipt=ToolReceipt(
                action=f"Commented on CompanyCam {target_type}",
                target=comment_target(content),
                url=parent_url,
            ),
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
            receipt=ToolReceipt(
                action="Tagged CompanyCam photo",
                target=tags_target(tag_names),
                url=photo_url(photo_id),
            ),
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
        return ToolResult(
            content=f"Photo {photo_id} permanently deleted.",
            receipt=ToolReceipt(
                action="Deleted CompanyCam photo",
                target=photo_target(None),
            ),
        )

    async def companycam_search_photos(
        project_id: str = "",
        start_date: str = "",
        end_date: str = "",
        page: int = 1,
    ) -> ToolResult:
        """Search photos across all CompanyCam projects."""
        start_ts: int | None = None
        end_ts: int | None = None
        if start_date:
            try:
                dt = datetime.fromisoformat(start_date)
                start_ts = int(calendar.timegm(dt.timetuple()))
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
            p_url = photo_url(p.id) or f"photo {p.id}"
            desc = f" - {p.description}" if p.description else ""
            lines.append(f"- ID: {p.id}{desc}: {p_url}")
        if len(photos) > 20:
            lines.append(f"(Showing 20 of {len(photos)})")
        if len(photos) >= 50:
            lines.append(f"(Page {page}. More results may be available on the next page.)")
        return ToolResult(content="\n".join(lines))

    return [
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
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                description_builder=lambda args: "Upload a photo to CompanyCam",
            ),
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
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                description_builder=lambda args: (
                    f"Add comment to CompanyCam {args.get('target_type', 'project')}"
                ),
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
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                description_builder=lambda args: "Add tags to a CompanyCam photo",
            ),
        ),
        Tool(
            name=ToolName.COMPANYCAM_DELETE_PHOTO,
            description=("WARNING: Permanently delete a CompanyCam photo. This cannot be undone."),
            function=companycam_delete_photo,
            params_model=CompanyCamDeletePhotoParams,
            usage_hint="Only delete a photo if the user explicitly asks.",
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                description_builder=lambda args: (
                    "Permanently delete a CompanyCam photo (cannot be undone)"
                ),
            ),
        ),
        Tool(
            name=ToolName.COMPANYCAM_SEARCH_PHOTOS,
            description="Search photos across all CompanyCam projects",
            function=companycam_search_photos,
            params_model=CompanyCamSearchPhotosParams,
            usage_hint="Find photos by project, date range, or browse recent photos.",
        ),
    ]
