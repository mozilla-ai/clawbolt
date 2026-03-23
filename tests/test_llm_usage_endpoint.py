"""Tests for GET /api/user/llm-usage endpoint."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient

import backend.app.database as _db_module
from backend.app.models import LLMUsageLog, User


def _create_usage(
    user_id: str,
    *,
    purpose: str = "chat",
    model: str = "test-model",
    input_tokens: int = 100,
    output_tokens: int = 50,
    cost: float = 0.001,
    created_at: datetime | None = None,
) -> None:
    db = _db_module.SessionLocal()
    try:
        db.add(
            LLMUsageLog(
                user_id=user_id,
                provider="test",
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
                cost=Decimal(str(cost)),
                purpose=purpose,
                created_at=created_at or datetime.now(UTC),
            )
        )
        db.commit()
    finally:
        db.close()


def _create_other_user() -> str:
    db = _db_module.SessionLocal()
    try:
        other = User(
            id=str(uuid.uuid4()),
            user_id="other-llm-user",
            phone="+15550002222",
            channel_identifier="777777777",
            preferred_channel="telegram",
        )
        db.add(other)
        db.commit()
        db.refresh(other)
        return other.id
    finally:
        db.close()


def test_llm_usage_empty(client: TestClient) -> None:
    """Returns zeroes when no usage exists."""
    resp = client.get("/api/user/llm-usage")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_calls"] == 0
    assert data["total_tokens"] == 0
    assert data["total_cost"] == 0.0
    assert data["by_purpose"] == []


def test_llm_usage_aggregation(client: TestClient, test_user: User) -> None:
    """Aggregates usage by purpose correctly."""
    _create_usage(test_user.id, purpose="chat", input_tokens=100, output_tokens=50, cost=0.01)
    _create_usage(test_user.id, purpose="chat", input_tokens=200, output_tokens=100, cost=0.02)
    _create_usage(
        test_user.id, purpose="heartbeat_decision", input_tokens=50, output_tokens=10, cost=0.005
    )

    resp = client.get("/api/user/llm-usage")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_calls"] == 3
    assert data["total_tokens"] == 510  # 150 + 300 + 60

    purposes = {p["purpose"]: p for p in data["by_purpose"]}
    assert "chat" in purposes
    assert purposes["chat"]["call_count"] == 2
    assert purposes["chat"]["total_input_tokens"] == 300
    assert purposes["chat"]["total_output_tokens"] == 150

    assert "heartbeat_decision" in purposes
    assert purposes["heartbeat_decision"]["call_count"] == 1


def test_llm_usage_scoped_to_user(client: TestClient, test_user: User) -> None:
    """Only includes usage for the authenticated user."""
    other_id = _create_other_user()
    _create_usage(test_user.id, purpose="chat", input_tokens=100, output_tokens=50)
    _create_usage(other_id, purpose="chat", input_tokens=999, output_tokens=999)

    resp = client.get("/api/user/llm-usage")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_calls"] == 1
    assert data["total_tokens"] == 150


def test_llm_usage_respects_days_param(client: TestClient, test_user: User) -> None:
    """Only includes usage within the specified number of days."""
    _create_usage(test_user.id, purpose="chat", created_at=datetime.now(UTC))
    _create_usage(
        test_user.id,
        purpose="old",
        created_at=datetime.now(UTC) - timedelta(days=60),
    )

    resp = client.get("/api/user/llm-usage?days=30")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_calls"] == 1
    purposes = {p["purpose"] for p in data["by_purpose"]}
    assert "chat" in purposes
    assert "old" not in purposes
