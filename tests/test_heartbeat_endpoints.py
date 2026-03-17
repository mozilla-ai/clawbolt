"""Tests for heartbeat endpoints."""

from fastapi.testclient import TestClient


def test_list_heartbeat_empty(client: TestClient) -> None:
    """Heartbeat listing returns empty when no items exist."""
    resp = client.get("/api/user/heartbeat")
    assert resp.status_code == 200
    items = resp.json()
    assert isinstance(items, list)


def test_create_heartbeat_item(client: TestClient) -> None:
    resp = client.post(
        "/api/user/heartbeat",
        json={"description": "Check job site"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["description"] == "Check job site"
    assert data["schedule"] == "daily"
    assert data["status"] == "active"
    assert data["id"]


def test_create_heartbeat_item_custom_schedule(client: TestClient) -> None:
    resp = client.post(
        "/api/user/heartbeat",
        json={"description": "Weekly review", "schedule": "weekdays"},
    )
    assert resp.status_code == 201
    assert resp.json()["schedule"] == "weekdays"


def test_list_after_create(client: TestClient) -> None:
    client.post("/api/user/heartbeat", json={"description": "Item 1"})
    client.post("/api/user/heartbeat", json={"description": "Item 2"})
    resp = client.get("/api/user/heartbeat")
    assert resp.status_code == 200
    descriptions = [i["description"] for i in resp.json()]
    assert "Item 1" in descriptions
    assert "Item 2" in descriptions


def test_delete_heartbeat_item(client: TestClient) -> None:
    resp = client.post("/api/user/heartbeat", json={"description": "To delete"})
    item_id = resp.json()["id"]
    resp = client.delete(f"/api/user/heartbeat/{item_id}")
    assert resp.status_code == 204
    # Verify it's gone
    resp = client.get("/api/user/heartbeat")
    descriptions = [i["description"] for i in resp.json()]
    assert "To delete" not in descriptions


def test_delete_heartbeat_item_not_found(client: TestClient) -> None:
    resp = client.delete("/api/user/heartbeat/9999")
    assert resp.status_code == 404


def test_create_heartbeat_empty_description(client: TestClient) -> None:
    resp = client.post("/api/user/heartbeat", json={"description": ""})
    assert resp.status_code == 422


def test_update_heartbeat_item(client: TestClient) -> None:
    resp = client.post("/api/user/heartbeat", json={"description": "Original"})
    item_id = resp.json()["id"]

    resp = client.put(
        f"/api/user/heartbeat/{item_id}",
        json={"description": "Updated", "schedule": "weekdays", "status": "paused"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["description"] == "Updated"
    assert data["schedule"] == "weekdays"
    assert data["status"] == "paused"


def test_update_heartbeat_partial(client: TestClient) -> None:
    """Only provided fields should change."""
    resp = client.post(
        "/api/user/heartbeat",
        json={"description": "My task", "schedule": "daily"},
    )
    item_id = resp.json()["id"]

    resp = client.put(
        f"/api/user/heartbeat/{item_id}",
        json={"status": "completed"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["description"] == "My task"
    assert data["schedule"] == "daily"
    assert data["status"] == "completed"


def test_update_heartbeat_not_found(client: TestClient) -> None:
    resp = client.put(
        "/api/user/heartbeat/9999",
        json={"description": "nope"},
    )
    assert resp.status_code == 404
