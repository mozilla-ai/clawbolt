from backend.app.agent.tools.base import Tool
from backend.app.services.twilio_service import TwilioService


def create_twilio_tools(twilio_service: TwilioService, to_number: str) -> list[Tool]:
    """Create Twilio-related tools for the agent."""

    async def send_reply(message: str) -> str:
        """Send an SMS reply to the contractor."""
        sid = await twilio_service.send_sms(to=to_number, body=message)
        return f"Sent SMS (SID: {sid})"

    async def send_media_reply(message: str, media_url: str) -> str:
        """Send an MMS reply with a media attachment."""
        sid = await twilio_service.send_mms(to=to_number, body=message, media_url=media_url)
        return f"Sent MMS (SID: {sid})"

    return [
        Tool(
            name="send_reply",
            description="Send an SMS text reply to the contractor.",
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
            description="Send an MMS reply with a media attachment (e.g., PDF estimate).",
            function=send_media_reply,
            parameters={
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "The message text"},
                    "media_url": {"type": "string", "description": "URL of the media to attach"},
                },
                "required": ["message", "media_url"],
            },
        ),
    ]
