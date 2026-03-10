from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolResult, ToolTags
from backend.app.agent.tools.names import ToolName

if TYPE_CHECKING:
    from backend.app.agent.tools.registry import ToolContext
    from backend.app.bus import OutboundMessage


class SendReplyParams(BaseModel):
    """Parameters for the send_reply tool."""

    message: str = Field(description="The message text to send")


class SendMediaReplyParams(BaseModel):
    """Parameters for the send_media_reply tool."""

    message: str = Field(description="The message text")
    media_url: str = Field(description="URL of the media to attach")


def create_messaging_tools(
    publish_outbound: Callable[[OutboundMessage], Awaitable[None]],
    channel: str,
    to_address: str,
) -> list[Tool]:
    """Create messaging tools for the agent."""

    async def send_reply(message: str) -> ToolResult:
        """Send a text reply to the user."""
        from backend.app.bus import OutboundMessage as OMsg

        if not message or not message.strip():
            return ToolResult(
                content="Error: message cannot be empty.",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )
        outbound = OMsg(channel=channel, chat_id=to_address, content=message)
        await publish_outbound(outbound)
        return ToolResult(content="Sent message")

    async def send_media_reply(message: str, media_url: str) -> ToolResult:
        """Send a reply with a media attachment."""
        from backend.app.bus import OutboundMessage as OMsg

        if not media_url or not media_url.strip():
            return ToolResult(
                content="Error: media_url cannot be empty.",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )
        outbound = OMsg(channel=channel, chat_id=to_address, content=message, media=[media_url])
        await publish_outbound(outbound)
        return ToolResult(content="Sent media message")

    return [
        Tool(
            name=ToolName.SEND_REPLY,
            description="Send a text reply to the user.",
            function=send_reply,
            params_model=SendReplyParams,
            tags={ToolTags.SENDS_REPLY},
            usage_hint="Use this to send a text message to the user.",
        ),
        Tool(
            name=ToolName.SEND_MEDIA_REPLY,
            description="Send a reply with a media attachment (e.g., PDF estimate).",
            function=send_media_reply,
            params_model=SendMediaReplyParams,
            tags={ToolTags.SENDS_REPLY},
            usage_hint=("When sending estimates or files, use this to send media to the user."),
        ),
    ]


def _messaging_factory(ctx: ToolContext) -> list[Tool]:
    """Factory for messaging tools, used by the registry."""
    assert ctx.publish_outbound is not None
    return create_messaging_tools(
        ctx.publish_outbound, channel=ctx.channel, to_address=ctx.to_address
    )


def _register() -> None:
    from backend.app.agent.tools.registry import default_registry

    default_registry.register("messaging", _messaging_factory, requires_outbound=True)


_register()
