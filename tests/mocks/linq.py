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
    direction: str = "inbound",
) -> dict:
    """Build a Linq webhook JSON payload (2026-02-03 format)."""
    parts: list[dict] = []
    if text:
        parts.append({"type": "text", "value": text})
    if media_url:
        parts.append({"type": "media", "url": media_url, "value": ""})

    return {
        "webhook_version": "2026-02-03",
        "event_id": f"evt-{message_id}",
        "type": event,
        "direction": direction,
        "sender_handle": sender,
        "chat": {
            "id": chat_id,
            "is_group": False,
            "owner_handle": "+15550000000",
        },
        "id": message_id,
        "parts": parts,
        "sent_at": "2026-03-24T12:00:00Z",
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
        "X-Webhook-Signature": signature,
        "X-Webhook-Timestamp": ts,
        "Content-Type": "application/json",
    }
