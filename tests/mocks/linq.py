import hashlib
import hmac
import time

LINQ_TEST_SIGNING_SECRET = "test-linq-signing-secret-12345"


def make_linq_webhook_payload(
    sender: str = "+15551234567",
    text: str = "Hello Clawbolt",
    media_url: str | None = None,
    event: str = "message.received",
    chat_id: str = "chat-uuid-001",
    message_id: str = "msg-uuid-001",
    is_from_me: bool = False,
    service: str = "iMessage",
) -> dict:
    """Build a realistic Linq webhook JSON payload."""
    parts: list[dict] = []
    if text:
        parts.append({"type": "text", "value": text})
    if media_url:
        parts.append({"type": "media", "url": media_url, "value": ""})

    return {
        "event": event,
        "data": {
            "id": message_id,
            "chat_id": chat_id,
            "from_handle": {
                "handle": sender,
                "service": service,
                "is_me": is_from_me,
            },
            "parts": parts,
            "is_from_me": is_from_me,
        },
    }


def make_linq_webhook_headers(
    payload_bytes: bytes,
    signing_secret: str = LINQ_TEST_SIGNING_SECRET,
    timestamp: int | None = None,
) -> dict[str, str]:
    """Generate valid HMAC webhook headers for a Linq payload."""
    ts = str(timestamp if timestamp is not None else int(time.time()))
    signature = hmac.new(
        key=signing_secret.encode(),
        msg=f"{ts}.{payload_bytes.decode()}".encode(),
        digestmod=hashlib.sha256,
    ).hexdigest()
    return {
        "X-Linq-Signature": signature,
        "X-Linq-Timestamp": ts,
        "Content-Type": "application/json",
    }
