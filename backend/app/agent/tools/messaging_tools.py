from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolResult, ToolTags
from backend.app.services.messaging import MessagingService

if TYPE_CHECKING:
    from backend.app.agent.tools.registry import ToolContext


class SendReplyParams(BaseModel):
    """Parameters for the send_reply tool."""

    message: str = Field(description="The message text to send")


class SendMediaReplyParams(BaseModel):
    """Parameters for the send_media_reply tool."""

    message: str = Field(description="The message text")
    media_url: str = Field(description="URL of the media to attach")


def create_messaging_tools(messaging_service: MessagingService, to_address: str) -> list[Tool]:
    """Create messaging tools for the agent."""

    async def send_reply(message: str) -> ToolResult:
        """Send a text reply to the contractor."""
        if not message or not message.strip():
            return ToolResult(
                content="Error: message cannot be empty.",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )
        msg_id = await messaging_service.send_text(to=to_address, body=message)
        return ToolResult(content=f"Sent message (ID: {msg_id})")

    async def send_media_reply(message: str, media_url: str) -> ToolResult:
        """Send a reply with a media attachment."""
        if not media_url or not media_url.strip():
            return ToolResult(
                content="Error: media_url cannot be empty.",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )
        msg_id = await messaging_service.send_media(
            to=to_address, body=message, media_url=media_url
        )
        return ToolResult(content=f"Sent media message (ID: {msg_id})")

    return [
        Tool(
            name="send_reply",
            description="Send a text reply to the contractor.",
            function=send_reply,
            params_model=SendReplyParams,
            tags={ToolTags.SENDS_REPLY},
            usage_hint="Use this to send a text message to the contractor.",
        ),
        Tool(
            name="send_media_reply",
            description="Send a reply with a media attachment (e.g., PDF estimate).",
            function=send_media_reply,
            params_model=SendMediaReplyParams,
            tags={ToolTags.SENDS_REPLY},
            usage_hint=(
                "When sending estimates or files, use this to send media to the contractor."
            ),
        ),
    ]


def _messaging_factory(ctx: ToolContext) -> list[Tool]:
    """Factory for messaging tools, used by the registry."""
    assert ctx.messaging_service is not None
    return create_messaging_tools(ctx.messaging_service, to_address=ctx.to_address)


def _register() -> None:
    from backend.app.agent.tools.registry import default_registry

    default_registry.register("messaging", _messaging_factory, requires_messaging=True)


_register()
