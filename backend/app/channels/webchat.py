"""Web chat channel: browser-based chat via the dashboard.

Messages are submitted via POST and responses arrive asynchronously through
a Server-Sent Events (SSE) endpoint. The POST handler normalizes the message
into an ``InboundMessage``, publishes it to the message bus, and returns a
``request_id`` + ``session_id`` immediately. The frontend then opens an SSE
connection to ``/api/user/chat/events/{request_id}`` to receive the reply.

Supports file and image uploads via multipart/form-data. Uploaded files are
converted directly into ``DownloadedMedia`` objects (skipping the Telegram
download step) and processed through the same vision/audio pipeline.
"""

import asyncio
import collections.abc
import json
import re
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backend.app.agent.context import get_or_create_conversation
from backend.app.agent.ingestion import InboundMessage
from backend.app.auth.dependencies import get_current_user
from backend.app.bus import message_bus
from backend.app.channels.base import BaseChannel
from backend.app.config import settings
from backend.app.media.download import DEFAULT_MIME_TYPE, DownloadedMedia, generate_filename
from backend.app.models import User

_SESSION_ID_RE = re.compile(r"^[\w-]+_\d+(_[\w]+)?$")


class _ChatAccepted(BaseModel):
    request_id: str
    session_id: str


class WebChatChannel(BaseChannel):
    """Browser-based chat channel for the dashboard.

    Messages are sent via POST and responses arrive via SSE.
    The channel's outbound methods (send_text, etc.) are no-ops because
    responses are delivered through the message bus / SSE.
    """

    @property
    def name(self) -> str:
        return "webchat"

    def get_router(self) -> APIRouter:
        router = APIRouter(tags=["webchat"])

        @router.post("/user/chat", response_model=_ChatAccepted)
        async def send_chat_message(
            message: str = Form(default=""),
            session_id: str | None = Form(default=None),
            files: list[UploadFile] = File(default=[]),
            user: User = Depends(get_current_user),
        ) -> _ChatAccepted:
            """Accept a message, publish to bus, return request_id for SSE."""
            text = message.strip()

            if not text and not files:
                raise HTTPException(status_code=422, detail="Either message text or files required")

            if session_id is not None and not _SESSION_ID_RE.match(session_id):
                raise HTTPException(
                    status_code=422,
                    detail="session_id must match pattern: digits_digits or digits_digits_digits",
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

            # Get/create session so we can return session_id immediately
            session, _ = await get_or_create_conversation(
                user.id,
                external_session_id=session_id,
            )

            request_id = str(uuid.uuid4())

            # Register response future and event queue before publishing so
            # the dispatcher can resolve it even if processing is very fast.
            message_bus.set_request_owner(request_id, str(user.id))
            message_bus.register_response_future(request_id)
            message_bus.register_event_queue(request_id)

            inbound = InboundMessage(
                channel="webchat",
                sender_id=str(user.id),
                text=text,
                downloaded_media=downloaded_media,
                request_id=request_id,
                session_id=session.session_id,
            )
            await message_bus.publish_inbound(inbound)

            return _ChatAccepted(request_id=request_id, session_id=session.session_id)

        @router.get("/user/chat/events/{request_id}")
        async def chat_events(
            request_id: str,
            user: User = Depends(get_current_user),
        ) -> StreamingResponse:
            """SSE endpoint: streams tool-call events then the final reply."""
            if not message_bus.check_request_owner(request_id, str(user.id)):
                raise HTTPException(status_code=403, detail="Not your request")

            async def event_stream() -> collections.abc.AsyncIterator[str]:
                queue = message_bus.register_event_queue(request_id)
                try:
                    fut = message_bus.get_response_future(request_id)
                    if fut is None:
                        fut = message_bus.register_response_future(request_id)

                    timeout = settings.approval_timeout_seconds
                    loop = asyncio.get_running_loop()
                    deadline = loop.time() + timeout

                    # Stream intermediate events until the final reply is ready
                    while not fut.done():
                        remaining = deadline - loop.time()
                        if remaining <= 0:
                            yield f"data: {json.dumps({'error': 'Response timed out'})}\n\n"
                            return
                        try:
                            event = await asyncio.wait_for(queue.get(), timeout=min(remaining, 1.0))
                            yield f"data: {json.dumps(event)}\n\n"
                        except TimeoutError:
                            continue

                    # Drain any events queued after the future resolved
                    while not queue.empty():
                        event = queue.get_nowait()
                        yield f"data: {json.dumps(event)}\n\n"

                    # Send the final reply
                    try:
                        outbound = fut.result()
                        yield f"data: {json.dumps({'reply': outbound.content})}\n\n"
                    except Exception:
                        yield f"data: {json.dumps({'error': 'Response failed'})}\n\n"
                finally:
                    message_bus.remove_event_queue(request_id)

            return StreamingResponse(
                event_stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        @router.get("/user/chat/activity")
        async def chat_activity(
            user: User = Depends(get_current_user),
        ) -> StreamingResponse:
            """SSE endpoint: streams agent activity (thinking, tool use) for this user.

            Delivers real-time status regardless of which channel (Telegram,
            webchat, etc.) originated the message being processed. Multiple
            browser tabs can subscribe concurrently.
            """
            user_id = str(user.id)
            queue = message_bus.register_activity_queue(user_id)

            async def event_stream() -> collections.abc.AsyncIterator[str]:
                try:
                    while True:
                        try:
                            event = await asyncio.wait_for(queue.get(), timeout=15.0)
                            yield f"data: {json.dumps(event)}\n\n"
                        except TimeoutError:
                            # Send keepalive comment to detect disconnected clients
                            yield ": keepalive\n\n"
                finally:
                    message_bus.remove_activity_queue(user_id, queue)

            return StreamingResponse(
                event_stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        return router

    def is_allowed(self, sender_id: str, username: str) -> bool:
        return True

    async def send_text(self, to: str, body: str) -> str:
        """No-op: web chat delivers responses via the message bus / SSE."""
        return ""

    async def send_media(self, to: str, body: str, media_url: str) -> str:
        """No-op: web chat delivers responses via the message bus / SSE."""
        return ""

    async def send_message(self, to: str, body: str, media_urls: list[str] | None = None) -> str:
        """No-op: web chat delivers responses via the message bus / SSE."""
        return ""

    async def send_typing_indicator(self, to: str) -> None:
        """No-op: typing state handled client-side."""

    async def download_media(self, file_id: str) -> DownloadedMedia:
        """Web chat does not support media downloads via file_id."""
        msg = "Web chat receives uploads directly, not file_id references"
        raise NotImplementedError(msg)
