"""Test helpers for BlueBubbles webhook payloads."""


def make_bluebubbles_webhook_payload(
    sender: str = "+15551234567",
    text: str = "Hello Clawbolt",
    event_type: str = "new-message",
    chat_guid: str = "iMessage;-;+15551234567",
    message_guid: str = "msg-guid-001",
    is_from_me: bool = False,
    attachments: list[dict] | None = None,
) -> dict:
    """Build a BlueBubbles webhook JSON payload matching the real server format."""
    data: dict = {
        "guid": message_guid,
        "text": text,
        "isFromMe": is_from_me,
        "isAudioMessage": False,
        "handle": {
            "address": sender,
            "service": "iMessage",
        },
        "chats": [
            {
                "guid": chat_guid,
            }
        ],
        "attachments": attachments or [],
    }

    return {
        "type": event_type,
        "data": data,
    }
