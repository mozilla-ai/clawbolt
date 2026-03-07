"""Tests for checklist endpoints."""

from fastapi.testclient import TestClient


def test_list_checklist_empty(client: TestClient) -> None:
    resp = client.get("/api/contractor/checklist")
    assert resp.status_code == 200
    assert resp.json() == []


def test_create_checklist_item(client: TestClient) -> None:
    resp = client.post(
        "/api/contractor/checklist",
        json={"description": "Check job site"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["description"] == "Check job site"
    assert data["schedule"] == "daily"
    assert data["status"] == "active"
    assert data["id"] > 0


def test_create_checklist_item_custom_schedule(client: TestClient) -> None:
    resp = client.post(
        "/api/contractor/checklist",
        json={"description": "Weekly review", "schedule": "weekdays"},
    )
    assert resp.status_code == 201
    assert resp.json()["schedule"] == "weekdays"


def test_list_after_create(client: TestClient) -> None:
    client.post("/api/contractor/checklist", json={"description": "Item 1"})
    client.post("/api/contractor/checklist", json={"description": "Item 2"})
    resp = client.get("/api/contractor/checklist")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


def test_delete_checklist_item(client: TestClient) -> None:
    resp = client.post("/api/contractor/checklist", json={"description": "To delete"})
    item_id = resp.json()["id"]
    resp = client.delete(f"/api/contractor/checklist/{item_id}")
    assert resp.status_code == 204
    # Verify it's gone
    resp = client.get("/api/contractor/checklist")
    assert len(resp.json()) == 0


def test_delete_checklist_item_not_found(client: TestClient) -> None:
    resp = client.delete("/api/contractor/checklist/9999")
    assert resp.status_code == 404


def test_create_checklist_empty_description(client: TestClient) -> None:
    resp = client.post("/api/contractor/checklist", json={"description": ""})
    assert resp.status_code == 422
