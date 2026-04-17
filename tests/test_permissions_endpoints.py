"""Tests for permissions endpoints (PERMISSIONS.json via API)."""

import json

from fastapi.testclient import TestClient

from backend.app.models import User


def test_get_permissions_returns_complete(client: TestClient, test_user: User) -> None:
    """GET /api/user/permissions returns a complete PERMISSIONS.json."""
    resp = client.get("/api/user/permissions")
    assert resp.status_code == 200
    data = resp.json()
    assert "content" in data
    parsed = json.loads(data["content"])
    assert "version" in parsed
    assert "tools" in parsed
    assert len(parsed["tools"]) > 0


def test_update_permissions(client: TestClient, test_user: User) -> None:
    """PUT /api/user/permissions overwrites the file and returns updated content."""
    # Get current permissions
    resp = client.get("/api/user/permissions")
    parsed = json.loads(resp.json()["content"])

    # Modify a permission
    parsed["tools"]["send_media_reply"] = "deny"
    resp = client.put(
        "/api/user/permissions",
        json={"content": json.dumps(parsed)},
    )
    assert resp.status_code == 200
    updated = json.loads(resp.json()["content"])
    assert updated["tools"]["send_media_reply"] == "deny"

    # Verify it persisted
    resp2 = client.get("/api/user/permissions")
    persisted = json.loads(resp2.json()["content"])
    assert persisted["tools"]["send_media_reply"] == "deny"


def test_update_permissions_invalid_json(client: TestClient, test_user: User) -> None:
    """PUT with invalid JSON returns 400."""
    resp = client.put(
        "/api/user/permissions",
        json={"content": "not valid json{{{"},
    )
    assert resp.status_code == 400
    assert "Invalid JSON" in resp.json()["detail"]


def test_update_permissions_non_dict(client: TestClient, test_user: User) -> None:
    """PUT with a JSON array (not object) returns 400."""
    resp = client.put(
        "/api/user/permissions",
        json={"content": "[1, 2, 3]"},
    )
    assert resp.status_code == 400
    assert "JSON object" in resp.json()["detail"]


def test_update_permissions_invalid_level(client: TestClient, test_user: User) -> None:
    """PUT with an invalid permission level returns 400."""
    payload = {"version": 1, "tools": {"send_media_reply": "yolo"}, "resources": {}}
    resp = client.put(
        "/api/user/permissions",
        json={"content": json.dumps(payload)},
    )
    assert resp.status_code == 400
    assert "Invalid permission level" in resp.json()["detail"]
    assert "send_media_reply" in resp.json()["detail"]


def test_update_permissions_invalid_tools_type(client: TestClient, test_user: User) -> None:
    """PUT with tools as a non-dict returns 400."""
    payload = {"version": 1, "tools": "not_a_dict", "resources": {}}
    resp = client.put(
        "/api/user/permissions",
        json={"content": json.dumps(payload)},
    )
    assert resp.status_code == 400
    assert "'tools' must be an object" in resp.json()["detail"]


def test_update_permissions_invalid_resource_level(client: TestClient, test_user: User) -> None:
    """PUT with an invalid resource permission level returns 400."""
    payload = {
        "version": 1,
        "tools": {},
        "resources": {"web_fetch": {"evil.com": "nope"}},
    }
    resp = client.put(
        "/api/user/permissions",
        json={"content": json.dumps(payload)},
    )
    assert resp.status_code == 400
    assert "Invalid permission level" in resp.json()["detail"]
