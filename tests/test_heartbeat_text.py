"""Tests for heartbeat_text field via the profile endpoint (HEARTBEAT.md)."""

from fastapi.testclient import TestClient

import backend.app.database as _db_module
from backend.app.models import User


def test_profile_includes_heartbeat_text(client: TestClient) -> None:
    """Profile response should include the heartbeat_text field."""
    resp = client.get("/api/user/profile")
    assert resp.status_code == 200
    data = resp.json()
    assert "heartbeat_text" in data


def test_update_heartbeat_text(client: TestClient) -> None:
    """Saving heartbeat_text via profile update should persist it."""
    heartbeat = "- [ ] Follow up with leads\n- [ ] Check job site"
    resp = client.put(
        "/api/user/profile",
        json={"heartbeat_text": heartbeat},
    )
    assert resp.status_code == 200
    assert resp.json()["heartbeat_text"] == heartbeat


def test_heartbeat_text_persists_in_db(client: TestClient) -> None:
    """Updating heartbeat_text should persist in the database."""
    heartbeat = "- [ ] Review pending estimates"
    resp = client.put("/api/user/profile", json={"heartbeat_text": heartbeat})
    assert resp.status_code == 200
    assert resp.json()["heartbeat_text"] == heartbeat


async def test_heartbeat_text_round_trip_via_db() -> None:
    """Writing heartbeat_text via the DB and reading it back should work."""
    db = _db_module.SessionLocal()
    try:
        user = User(user_id="heartbeat-test", phone="+15551112222")
        db.add(user)
        db.commit()
        db.refresh(user)
        user_id = user.id
        db.expunge(user)
    finally:
        db.close()

    # Update with heartbeat text
    db = _db_module.SessionLocal()
    try:
        db_user = db.query(User).filter_by(id=user_id).first()
        assert db_user is not None
        db_user.heartbeat_text = "- [ ] Test item"
        db.commit()
        db.refresh(db_user)
        db.expunge(db_user)
        updated = db_user
    finally:
        db.close()
    assert updated.heartbeat_text == "- [ ] Test item"

    # Re-read from DB
    db = _db_module.SessionLocal()
    try:
        reloaded = db.query(User).filter_by(id=user_id).first()
        assert reloaded is not None
        db.expunge(reloaded)
    finally:
        db.close()
    assert reloaded.heartbeat_text == "- [ ] Test item"


async def test_new_user_heartbeat_text_empty() -> None:
    """New users should have empty heartbeat_text by default."""
    db = _db_module.SessionLocal()
    try:
        user = User(user_id="default-heartbeat-test", phone="+15559998888")
        db.add(user)
        db.commit()
        db.refresh(user)
        db.expunge(user)
    finally:
        db.close()
    assert user.heartbeat_text == ""
