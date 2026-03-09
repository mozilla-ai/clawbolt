"""Tests for the web chat channel endpoint (async bus + SSE flow)."""

import io
from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.app.agent.file_store import UserData, get_user_store
from backend.app.bus import OutboundMessage, message_bus
from backend.app.config import settings
from backend.app.main import app


@pytest.fixture()
async def webchat_user() -> UserData:
    """Create a user for web chat tests."""
    store = get_user_store()
    return await store.create(
        user_id="webchat-test-user",
        name="Test User",
    )


@pytest.fixture()
def webchat_client(webchat_user: UserData) -> Generator[TestClient]:
    """TestClient that uses real get_current_user (no auth override).

    Mocks the LLM to avoid external API calls during tests.
    """
    with (
        patch("backend.app.main._verify_llm_settings", new_callable=AsyncMock),
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.start"),
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.stop"),
        patch("backend.app.channels.telegram.settings.telegram_bot_token", ""),
        patch("backend.app.agent.ingestion.settings.message_batch_window_ms", 0),
        TestClient(app) as c,
    ):
        yield c
    app.dependency_overrides.clear()


def test_chat_endpoint_returns_request_id(
    webchat_client: TestClient,
    webchat_user: UserData,
) -> None:
    """POST /api/user/chat should return request_id and session_id."""
    resp = webchat_client.post(
        "/api/user/chat",
        data={"message": "Hi there"},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert "request_id" in data
    assert "session_id" in data
    assert len(data["request_id"]) > 0


def test_chat_endpoint_missing_body(webchat_client: TestClient) -> None:
    """Empty request (no message, no files) should return 422."""
    resp = webchat_client.post("/api/user/chat", data={"message": ""})
    assert resp.status_code == 422


def test_chat_returns_same_session(
    webchat_client: TestClient,
    webchat_user: UserData,
) -> None:
    """Multiple messages within the session timeout should use the same session."""
    resp1 = webchat_client.post(
        "/api/user/chat",
        data={"message": "First message"},
    )
    resp2 = webchat_client.post(
        "/api/user/chat",
        data={"message": "Second message"},
    )

    assert resp1.json()["session_id"] == resp2.json()["session_id"]


def test_chat_with_explicit_session_id(
    webchat_client: TestClient,
    webchat_user: UserData,
) -> None:
    """Sending session_id should resume that session."""
    resp1 = webchat_client.post(
        "/api/user/chat",
        data={"message": "First message"},
    )
    session_id = resp1.json()["session_id"]

    resp2 = webchat_client.post(
        "/api/user/chat",
        data={"message": "Second message", "session_id": session_id},
    )

    assert resp2.status_code == 200
    assert resp2.json()["session_id"] == session_id


def test_chat_with_invalid_session_id(webchat_client: TestClient) -> None:
    """Invalid session_id format should return 422."""
    resp = webchat_client.post(
        "/api/user/chat",
        data={"message": "Hello", "session_id": "../../bad"},
    )
    assert resp.status_code == 422


def test_chat_with_nonexistent_session_id(
    webchat_client: TestClient,
    webchat_user: UserData,
) -> None:
    """Valid-format but nonexistent session_id should create a new session."""
    resp = webchat_client.post(
        "/api/user/chat",
        data={"message": "Hello", "session_id": "9999_9999"},
    )

    assert resp.status_code == 200
    # A new session should have been created (not the requested one)
    assert resp.json()["session_id"] != "9999_9999"


# ---------------------------------------------------------------------------
# File upload tests
# ---------------------------------------------------------------------------


def test_chat_with_image_upload(
    webchat_client: TestClient,
    webchat_user: UserData,
) -> None:
    """Upload an image with text; verify the request is accepted."""
    # 1x1 red PNG
    png_bytes = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00"
        b"\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00"
        b"\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    resp = webchat_client.post(
        "/api/user/chat",
        data={"message": "Check this out"},
        files=[("files", ("photo.png", io.BytesIO(png_bytes), "image/png"))],
    )

    assert resp.status_code == 200
    data = resp.json()
    assert "request_id" in data
    assert "session_id" in data


def test_chat_with_files_only(
    webchat_client: TestClient,
    webchat_user: UserData,
) -> None:
    """Upload a file without text; should still succeed."""
    resp = webchat_client.post(
        "/api/user/chat",
        files=[("files", ("doc.pdf", io.BytesIO(b"%PDF-1.4 test"), "application/pdf"))],
    )

    assert resp.status_code == 200
    assert "request_id" in resp.json()


def test_chat_no_message_no_files(webchat_client: TestClient) -> None:
    """Empty request with no text and no files should return 422."""
    resp = webchat_client.post("/api/user/chat", data={"message": "  "})
    assert resp.status_code == 422


def test_chat_file_too_large(
    webchat_client: TestClient,
    webchat_user: UserData,
) -> None:
    """Oversized file should return 422."""
    with patch.object(settings, "max_media_size_bytes", 10):
        resp = webchat_client.post(
            "/api/user/chat",
            data={"message": "here is a big file"},
            files=[("files", ("big.bin", io.BytesIO(b"x" * 100), "application/octet-stream"))],
        )

    assert resp.status_code == 422
    assert "too large" in resp.json()["detail"].lower()


def test_chat_multiple_files(
    webchat_client: TestClient,
    webchat_user: UserData,
) -> None:
    """Multiple files in one request should all be accepted."""
    resp = webchat_client.post(
        "/api/user/chat",
        data={"message": "Multiple attachments"},
        files=[
            ("files", ("a.png", io.BytesIO(b"img1"), "image/png")),
            ("files", ("b.pdf", io.BytesIO(b"pdf1"), "application/pdf")),
            ("files", ("c.mp3", io.BytesIO(b"audio1"), "audio/mpeg")),
        ],
    )

    assert resp.status_code == 200
    assert "request_id" in resp.json()


# ---------------------------------------------------------------------------
# Bus integration: verify inbound messages reach the bus
# ---------------------------------------------------------------------------


def test_chat_publishes_to_bus(
    webchat_client: TestClient,
    webchat_user: UserData,
) -> None:
    """POST /api/user/chat should publish an InboundMessage to the bus."""
    with patch(
        "backend.app.channels.webchat.message_bus.publish_inbound",
        new_callable=AsyncMock,
    ) as mock_pub:
        resp = webchat_client.post(
            "/api/user/chat",
            data={"message": "Bus test"},
        )

    assert resp.status_code == 200
    mock_pub.assert_called_once()
    inbound = mock_pub.call_args[0][0]
    assert inbound.channel == "webchat"
    assert inbound.text == "Bus test"


# ---------------------------------------------------------------------------
# SSE endpoint tests
# ---------------------------------------------------------------------------


def test_sse_endpoint_returns_reply(
    webchat_client: TestClient,
    webchat_user: UserData,
) -> None:
    """GET /api/user/chat/events/{request_id} should stream the reply as SSE."""
    import threading

    # Mock bus publish so the consumer doesn't process the message
    with patch(
        "backend.app.channels.webchat.message_bus.publish_inbound",
        new_callable=AsyncMock,
    ):
        resp = webchat_client.post(
            "/api/user/chat",
            data={"message": "SSE test"},
        )
    assert resp.status_code == 200
    request_id = resp.json()["request_id"]

    # The POST registered a response future. Resolve it from another thread
    # shortly after the SSE stream opens.
    outbound = OutboundMessage(
        channel="webchat", chat_id="1", content="Hello from agent!", request_id=request_id
    )

    def _resolve() -> None:
        import time

        time.sleep(0.2)
        message_bus.resolve_response(request_id, outbound)

    t = threading.Thread(target=_resolve)
    t.start()

    with webchat_client.stream("GET", f"/api/user/chat/events/{request_id}") as sse_resp:
        assert sse_resp.status_code == 200
        body = b""
        for chunk in sse_resp.iter_bytes():
            body += chunk
        text = body.decode()
        assert "Hello from agent!" in text

    t.join(timeout=5)
