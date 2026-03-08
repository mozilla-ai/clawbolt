"""Tests for contractor profile endpoints."""

from fastapi.testclient import TestClient


def test_get_profile(client: TestClient) -> None:
    resp = client.get("/api/user/profile")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Test Contractor"
    assert data["trade"] == "General Contractor"
    assert data["location"] == "Portland, OR"
    assert data["assistant_name"] == "Clawbolt"
    assert data["onboarding_complete"] is False
    assert data["is_active"] is True
    assert "created_at" in data
    assert "updated_at" in data


def test_update_profile_partial(client: TestClient) -> None:
    resp = client.put(
        "/api/user/profile",
        json={"name": "Updated Name", "hourly_rate": 85.0},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Updated Name"
    assert data["hourly_rate"] == 85.0
    assert data["trade"] == "General Contractor"


def test_update_profile_soul_text(client: TestClient) -> None:
    resp = client.put(
        "/api/user/profile",
        json={"soul_text": "Be friendly.", "assistant_name": "Bolt"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["soul_text"] == "Be friendly."
    assert data["assistant_name"] == "Bolt"


def test_update_profile_empty_body(client: TestClient) -> None:
    resp = client.put("/api/user/profile", json={})
    assert resp.status_code == 400


def test_update_returns_updated_values(client: TestClient) -> None:
    resp = client.put("/api/user/profile", json={"trade": "Electrician"})
    assert resp.status_code == 200
    assert resp.json()["trade"] == "Electrician"
