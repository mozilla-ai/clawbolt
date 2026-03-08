"""Web chat channel: browser-based chat via the dashboard.

Provides a POST endpoint for sending messages and receiving agent responses
synchronously. The dashboard chat UI calls this endpoint and displays the
response. Responses are also persisted in the session store so they appear
in the Conversations page.

Supports file and image uploads via multipart/form-data. Uploaded files are
converted directly into ``DownloadedMedia`` objects (skipping the Telegram
download step) and processed through the same vision/audio pipeline.
"""

import logging
import re

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from backend.app.agent.concurrency import contractor_locks
from backend.app.agent.context import get_or_create_conversation
from backend.app.agent.file_store import ContractorData, get_contractor_store, get_session_store
from backend.app.agent.router import (
    PipelineContext,
    build_context_step,
    finalize_onboarding_step,
    init_storage,
    load_history_step,
    persist_outbound_step,
    run_agent_step,
    run_pipeline,
)
from backend.app.agent.tools.file_tools import auto_save_media
from backend.app.auth.dependencies import get_current_user
from backend.app.channels.base import BaseChannel
from backend.app.config import settings
from backend.app.enums import MessageDirection
from backend.app.media.download import DEFAULT_MIME_TYPE, DownloadedMedia, generate_filename

logger = logging.getLogger(__name__)

_SESSION_ID_RE = re.compile(r"^\d+_\d+$")


class _ChatResponse(BaseModel):
    reply: str
    session_id: str


# Pipeline for web chat: same as default but without dispatch_reply_step
# (the reply is returned directly in the HTTP response) and without
# prepare_media_step (files arrive as uploads, not Telegram file_ids).
_WEBCHAT_PIPELINE = [
    build_context_step,
    load_history_step,
    run_agent_step,
    finalize_onboarding_step,
    persist_outbound_step,
]


class WebChatChannel(BaseChannel):
    """Browser-based chat channel for the dashboard.

    Messages are sent via POST and responses are returned synchronously.
    The channel's outbound methods (send_text, etc.) are no-ops because
    responses are returned directly in the HTTP response body.
    """

    @property
    def name(self) -> str:
        return "webchat"

    def get_router(self) -> APIRouter:
        router = APIRouter(tags=["webchat"])
        channel = self

        @router.post("/user/chat", response_model=_ChatResponse)
        async def send_chat_message(
            message: str = Form(default=""),
            session_id: str | None = Form(default=None),
            files: list[UploadFile] = File(default=[]),
            contractor: ContractorData = Depends(get_current_user),
        ) -> _ChatResponse:
            """Send a message and receive the agent's response."""
            text = message.strip()

            # Validate: at least one of text or files required
            if not text and not files:
                raise HTTPException(status_code=422, detail="Either message text or files required")

            # Validate session_id format
            if session_id is not None and not _SESSION_ID_RE.match(session_id):
                raise HTTPException(
                    status_code=422,
                    detail="session_id must match pattern: digits_digits",
                )

            # Build DownloadedMedia from uploaded files
            downloaded_media: list[DownloadedMedia] = []
            for upload in files:
                content = await upload.read()
                if len(content) > settings.max_media_size_bytes:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"File too large: {len(content)} bytes "
                            f"(limit {settings.max_media_size_bytes} bytes)"
                        ),
                    )
                mime = upload.content_type or DEFAULT_MIME_TYPE
                filename = upload.filename or generate_filename(mime)
                downloaded_media.append(
                    DownloadedMedia(
                        content=content,
                        mime_type=mime,
                        original_url=f"upload://{filename}",
                        filename=filename,
                    )
                )

            session, _ = await get_or_create_conversation(
                contractor.id, external_session_id=session_id
            )

            session_store = get_session_store(contractor.id)
            stored_message = await session_store.add_message(
                session=session,
                direction=MessageDirection.INBOUND,
                body=text,
            )

            # Initialize storage and auto-save uploaded media
            storage = init_storage(contractor)
            if storage and downloaded_media:
                try:
                    await auto_save_media(contractor, storage, downloaded_media)
                except Exception:
                    logger.debug("Auto-save to storage failed, continuing")

            ctx = PipelineContext(
                contractor=contractor,
                session=session,
                message=stored_message,
                media_urls=[],
                messaging_service=channel,
                to_address=str(contractor.id),
                downloaded_media=downloaded_media,
                storage=storage,
            )

            async with contractor_locks.acquire(contractor.id):
                # Reload contractor in case it was updated concurrently
                store = get_contractor_store()
                fresh = await store.get_by_id(contractor.id)
                if fresh is not None:
                    ctx.contractor = fresh
                ctx = await run_pipeline(ctx, _WEBCHAT_PIPELINE)

            reply = ctx.response.reply_text if ctx.response else ""
            return _ChatResponse(reply=reply, session_id=session.session_id)

        return router

    def is_allowed(self, sender_id: str, username: str) -> bool:
        return True

    async def send_text(self, to: str, body: str) -> str:
        """No-op: web chat returns responses via the HTTP response body."""
        return ""

    async def send_media(self, to: str, body: str, media_url: str) -> str:
        """No-op: web chat returns responses via the HTTP response body."""
        return ""

    async def send_message(self, to: str, body: str, media_urls: list[str] | None = None) -> str:
        """No-op: web chat returns responses via the HTTP response body."""
        return ""

    async def send_typing_indicator(self, to: str) -> None:
        """No-op: typing state handled client-side."""

    async def download_media(self, file_id: str) -> DownloadedMedia:
        """Web chat does not support media downloads via file_id."""
        msg = "Web chat receives uploads directly, not file_id references"
        raise NotImplementedError(msg)
