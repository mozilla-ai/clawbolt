from backend.app.agent.tools.base import Tool
from backend.app.services.messaging import MessagingService


def create_messaging_tools(messaging_service: MessagingService, to_address: str) -> list[Tool]:
    """Create messaging tools for the agent."""

    async def send_reply(message: str) -> str:
        """Send a text reply to the contractor."""
        if not message or not message.strip():
            return "Error: message cannot be empty."
        msg_id = await messaging_service.send_text(to=to_address, body=message)
        return f"Sent message (ID: {msg_id})"

    async def send_media_reply(message: str, media_url: str) -> str:
        """Send a reply with a media attachment."""
        if not media_url or not media_url.strip():
            return "Error: media_url cannot be empty."
        msg_id = await messaging_service.send_media(
            to=to_address, body=message, media_url=media_url
        )
        return f"Sent media message (ID: {msg_id})"

    return [
        Tool(
            name="send_reply",
            description="Send a text reply to the contractor.",
            function=send_reply,
            parameters={
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "The message text to send"},
                },
                "required": ["message"],
            },
        ),
        Tool(
            name="send_media_reply",
            description="Send a reply with a media attachment (e.g., PDF estimate).",
            function=send_media_reply,
            parameters={
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "The message text"},
                    "media_url": {
                        "type": "string",
                        "description": "URL of the media to attach",
                    },
                },
                "required": ["message", "media_url"],
            },
        ),
    ]
