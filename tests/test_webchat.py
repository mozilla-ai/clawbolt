"""Tests for the web chat channel endpoint."""

import json
from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.app.agent.file_store import ContractorData, get_contractor_store
from backend.app.config import settings
from backend.app.main import app


@pytest.fixture()
async def webchat_contractor() -> ContractorData:
    """Create a contractor for web chat tests."""
    store = get_contractor_store()
    return await store.create(
        user_id="webchat-test-user",
        name="Test Contractor",
        trade="Electrician",
    )


@pytest.fixture()
def webchat_client(webchat_contractor: ContractorData) -> Generator[TestClient]:
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


def test_chat_endpoint_returns_reply(
    webchat_client: TestClient,
    webchat_contractor: ContractorData,
) -> None:
    """POST /api/contractor/chat should return an agent reply."""
    with patch(
        "backend.app.agent.router.run_agent",
        new_callable=AsyncMock,
    ) as mock_agent:
        from backend.app.agent.core import AgentResponse

        mock_agent.return_value = AgentResponse(reply_text="Hello from the agent!")

        resp = webchat_client.post(
            "/api/contractor/chat",
            json={"message": "Hi there"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["reply"] == "Hello from the agent!"
    assert "session_id" in data


def test_chat_endpoint_persists_messages(
    webchat_client: TestClient,
    webchat_contractor: ContractorData,
) -> None:
    """Chat messages should be persisted in the session store."""
    with patch(
        "backend.app.agent.router.run_agent",
        new_callable=AsyncMock,
    ) as mock_agent:
        from backend.app.agent.core import AgentResponse

        mock_agent.return_value = AgentResponse(reply_text="Got it!")

        resp = webchat_client.post(
            "/api/contractor/chat",
            json={"message": "Remember my rate is 85"},
        )
        assert resp.status_code == 200
        session_id = resp.json()["session_id"]

    # Read the session JSONL file directly to verify persistence
    session_path = (
        Path(settings.contractor_data_dir)
        / str(webchat_contractor.id)
        / "sessions"
        / f"{session_id}.jsonl"
    )
    assert session_path.exists()
    lines = [json.loads(line) for line in session_path.read_text().strip().split("\n")]
    # First line is metadata, subsequent lines are messages
    message_lines = [line for line in lines if line.get("_type") != "metadata"]
    bodies = [m["body"] for m in message_lines]
    assert "Remember my rate is 85" in bodies
    assert "Got it!" in bodies


def test_chat_endpoint_missing_body(webchat_client: TestClient) -> None:
    """Missing body should return 422."""
    resp = webchat_client.post("/api/contractor/chat", json={})
    assert resp.status_code == 422


def test_chat_returns_same_session(
    webchat_client: TestClient,
    webchat_contractor: ContractorData,
) -> None:
    """Multiple messages within the session timeout should use the same session."""
    with patch(
        "backend.app.agent.router.run_agent",
        new_callable=AsyncMock,
    ) as mock_agent:
        from backend.app.agent.core import AgentResponse

        mock_agent.return_value = AgentResponse(reply_text="Reply 1")
        resp1 = webchat_client.post(
            "/api/contractor/chat",
            json={"message": "First message"},
        )

        mock_agent.return_value = AgentResponse(reply_text="Reply 2")
        resp2 = webchat_client.post(
            "/api/contractor/chat",
            json={"message": "Second message"},
        )

    assert resp1.json()["session_id"] == resp2.json()["session_id"]


def test_chat_with_explicit_session_id(
    webchat_client: TestClient,
    webchat_contractor: ContractorData,
) -> None:
    """Sending session_id should resume that session."""
    with patch(
        "backend.app.agent.router.run_agent",
        new_callable=AsyncMock,
    ) as mock_agent:
        from backend.app.agent.core import AgentResponse

        mock_agent.return_value = AgentResponse(reply_text="Reply 1")
        resp1 = webchat_client.post(
            "/api/contractor/chat",
            json={"message": "First message"},
        )
        session_id = resp1.json()["session_id"]

        mock_agent.return_value = AgentResponse(reply_text="Reply 2")
        resp2 = webchat_client.post(
            "/api/contractor/chat",
            json={"message": "Second message", "session_id": session_id},
        )

    assert resp2.status_code == 200
    assert resp2.json()["session_id"] == session_id


def test_chat_with_invalid_session_id(webchat_client: TestClient) -> None:
    """Invalid session_id format should return 422."""
    resp = webchat_client.post(
        "/api/contractor/chat",
        json={"message": "Hello", "session_id": "../../bad"},
    )
    assert resp.status_code == 422


def test_chat_with_nonexistent_session_id(
    webchat_client: TestClient,
    webchat_contractor: ContractorData,
) -> None:
    """Valid-format but nonexistent session_id should create a new session."""
    with patch(
        "backend.app.agent.router.run_agent",
        new_callable=AsyncMock,
    ) as mock_agent:
        from backend.app.agent.core import AgentResponse

        mock_agent.return_value = AgentResponse(reply_text="Hello!")
        resp = webchat_client.post(
            "/api/contractor/chat",
            json={"message": "Hello", "session_id": "9999_9999"},
        )

    assert resp.status_code == 200
    # A new session should have been created (not the requested one)
    assert resp.json()["session_id"] != "9999_9999"
