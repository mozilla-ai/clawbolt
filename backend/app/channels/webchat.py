"""Web chat channel: browser-based chat via the dashboard.

Provides a POST endpoint for sending messages and receiving agent responses
synchronously. The dashboard chat UI calls this endpoint and displays the
response. Responses are also persisted in the session store so they appear
in the Conversations page.
"""

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from backend.app.agent.concurrency import contractor_locks
from backend.app.agent.context import get_or_create_conversation
from backend.app.agent.file_store import ContractorData, get_contractor_store, get_session_store
from backend.app.agent.router import (
    PipelineContext,
    build_context_step,
    finalize_onboarding_step,
    load_history_step,
    persist_outbound_step,
    run_agent_step,
    run_pipeline,
)
from backend.app.auth.dependencies import get_current_user
from backend.app.channels.base import BaseChannel
from backend.app.enums import MessageDirection
from backend.app.media.download import DownloadedMedia

logger = logging.getLogger(__name__)


class _ChatRequest(BaseModel):
    message: str
    session_id: str | None = Field(default=None, pattern=r"^\d+_\d+$")


class _ChatResponse(BaseModel):
    reply: str
    session_id: str


# Pipeline for web chat: same as default but without dispatch_reply_step
# (the reply is returned directly in the HTTP response) and without
# prepare_media_step (web chat text-only for now).
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

        @router.post("/contractor/chat", response_model=_ChatResponse)
        async def send_chat_message(
            body: _ChatRequest,
            contractor: ContractorData = Depends(get_current_user),
        ) -> _ChatResponse:
            """Send a message and receive the agent's response."""
            session, _ = await get_or_create_conversation(
                contractor.id, external_session_id=body.session_id
            )

            session_store = get_session_store(contractor.id)
            message = await session_store.add_message(
                session=session,
                direction=MessageDirection.INBOUND,
                body=body.message,
            )

            ctx = PipelineContext(
                contractor=contractor,
                session=session,
                message=message,
                media_urls=[],
                messaging_service=channel,
                to_address=str(contractor.id),
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
        """Web chat does not support media uploads yet."""
        msg = "Web chat does not support media uploads"
        raise NotImplementedError(msg)
