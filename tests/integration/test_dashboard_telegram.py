"""Integration test: Telegram-created contractor data visible via dashboard API.

Regression test for https://github.com/mozilla-ai/clawbolt/issues/475.
Previously, `get_current_user` always created a new `local@clawbolt.local`
contractor, so the dashboard never showed data from Telegram sessions.

These tests use a TestClient that does NOT override `get_current_user`,
exercising the real auth dependency against a pre-populated store.
"""

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
def telegram_contractor() -> ContractorData:
    """Simulate a contractor created by Telegram ingestion."""
    import asyncio

    store = get_contractor_store()
    return asyncio.get_event_loop().run_until_complete(
        store.create(
            user_id="telegram_123456789",
            name="Telegram User",
            phone="+15551234567",
            trade="Electrician",
            location="Seattle, WA",
            channel_identifier="123456789",
            preferred_channel="telegram",
        )
    )


@pytest.fixture()
def real_auth_client(telegram_contractor: ContractorData) -> Generator[TestClient]:
    """TestClient that uses the real get_current_user (no auth override).

    This is the critical difference from the standard ``client`` fixture in
    conftest.py, which overrides ``get_current_user`` and therefore never
    exercises the logic that picks an existing contractor from the store.
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


def _create_session(
    contractor: ContractorData,
    session_id: str,
    messages: list[dict],
) -> None:
    """Write a JSONL session file for the given contractor."""
    base = Path(settings.contractor_data_dir) / str(contractor.id) / "sessions"
    base.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(
            {
                "_type": "metadata",
                "session_id": session_id,
                "contractor_id": contractor.id,
                "created_at": "2025-01-15T10:00:00+00:00",
                "last_message_at": "2025-01-15T10:05:00+00:00",
                "is_active": True,
                "last_compacted_seq": 0,
            }
        )
    ]
    for msg in messages:
        lines.append(json.dumps(msg))
    (base / f"{session_id}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _seed_memory(contractor: ContractorData) -> None:
    """Write a MEMORY.md for the given contractor."""
    mem_dir = Path(settings.contractor_data_dir) / str(contractor.id) / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    (mem_dir / "MEMORY.md").write_text(
        "# Long-term Memory\n\n"
        "## Business\n"
        "- hourly_rate: 95 (confidence: 1.0)\n"
        "- specialty: panel upgrades (confidence: 0.9)\n",
        encoding="utf-8",
    )


class TestDashboardSeesTelegramData:
    """Dashboard endpoints return the Telegram contractor's data."""

    def test_profile_returns_telegram_contractor(
        self,
        real_auth_client: TestClient,
        telegram_contractor: ContractorData,
    ) -> None:
        resp = real_auth_client.get("/api/contractor/profile")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Telegram User"
        assert data["trade"] == "Electrician"
        assert data["location"] == "Seattle, WA"

    def test_sessions_returns_telegram_sessions(
        self,
        real_auth_client: TestClient,
        telegram_contractor: ContractorData,
    ) -> None:
        _create_session(
            telegram_contractor,
            "1_100",
            [
                {
                    "direction": "inbound",
                    "body": "I need a panel upgrade quote",
                    "timestamp": "2025-01-15T10:01:00",
                    "seq": 1,
                },
                {
                    "direction": "outbound",
                    "body": "Sure, I can help with that.",
                    "timestamp": "2025-01-15T10:02:00",
                    "seq": 2,
                },
            ],
        )
        resp = real_auth_client.get("/api/contractor/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["sessions"][0]["id"] == "1_100"
        assert data["sessions"][0]["message_count"] == 2

    def test_memory_returns_telegram_facts(
        self,
        real_auth_client: TestClient,
        telegram_contractor: ContractorData,
    ) -> None:
        _seed_memory(telegram_contractor)
        resp = real_auth_client.get("/api/contractor/memory")
        assert resp.status_code == 200
        facts = resp.json()
        assert len(facts) == 2
        keys = {f["key"] for f in facts}
        assert "hourly_rate" in keys
        assert "specialty" in keys

    def test_stats_returns_telegram_stats(
        self,
        real_auth_client: TestClient,
        telegram_contractor: ContractorData,
    ) -> None:
        _create_session(
            telegram_contractor,
            "1_200",
            [
                {
                    "direction": "inbound",
                    "body": "Hello",
                    "timestamp": "2025-01-15T10:01:00",
                    "seq": 1,
                },
            ],
        )
        _seed_memory(telegram_contractor)
        resp = real_auth_client.get("/api/contractor/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_sessions"] == 1
        assert data["total_memory_facts"] == 2
