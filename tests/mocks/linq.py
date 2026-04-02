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
    """Build a Linq webhook JSON payload matching the real v3 2026-02-03 format."""
    parts: list[dict] = []
    if text:
        parts.append({"type": "text", "value": text})
    if media_url:
        parts.append({"type": "media", "url": media_url, "value": ""})

    return {
        "api_version": "v3",
        "webhook_version": "2026-02-03",
        "event_type": event,
        "event_id": f"evt-{message_id}",
        "created_at": "2026-03-24T12:00:00Z",
        "trace_id": "test-trace-id",
        "partner_id": "test-partner-id",
        "data": {
            "id": message_id,
            "direction": direction,
            "sender_handle": {
                "handle": sender,
                "id": "sender-uuid",
                "is_me": False,
                "service": "iMessage",
                "status": "active",
            },
            "chat": {
                "id": chat_id,
                "is_group": False,
                "owner_handle": {
                    "handle": "+15550000000",
                    "id": "owner-uuid",
                    "is_me": True,
                    "service": "iMessage",
                    "status": "active",
                },
            },
            "parts": parts,
            "service": "iMessage",
            "sent_at": "2026-03-24T12:00:00Z",
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
        msg=f"{ts}.{payload_bytes.decode('utf-8')}".encode(),
        digestmod=hashlib.sha256,
    ).hexdigest()
    return {
        "X-Webhook-Signature": signature,
        "X-Webhook-Timestamp": ts,
        "Content-Type": "application/json",
    }
