"""Tests for the admin LLM-payload export endpoint.

Covers the consent-gated read surface that downloads the captured
payloads stored by ``llm_payload_capture``. Uses the sync ``client``
fixture (admin auth via ``test_subscription``) and seeds the capture
row directly through ``db_session`` so the route's async read finds it.
"""

from __future__ import annotations

import datetime
import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.app.models import LLMPayloadCapture, Subscription, User


def _insert_target_user(db_session: Session, *, consent: bool) -> str:
    """Create a non-admin user that the admin endpoint will export from."""
    new_id = str(uuid.uuid4())
    user = User(
        id=new_id,
        user_id=f"target-{new_id[:8]}",
        phone="",
        channel_identifier=f"ch-{new_id[:8]}",
        preferred_channel="telegram",
        onboarding_complete=True,
        data_sharing_consent=consent,
    )
    # Flush the User first: without an ORM relationship between the two
    # models, one flush orders the INSERTs by mapper sort key and puts
    # subscriptions ahead of users, violating the FK.
    db_session.add(user)
    db_session.flush()
    db_session.add(Subscription(user_id=new_id, role="user", plan="free", status="active"))
    db_session.commit()
    return new_id


def _insert_capture(
    db_session: Session,
    *,
    user_id: str,
    with_previous: bool = False,
    with_response: bool = False,
) -> None:
    now = datetime.datetime.now(datetime.UTC)
    sample_payload = {
        "schema_version": 1,
        "model": "claude-test",
        "messages": [{"role": "user", "content": "hello"}],
    }
    row = LLMPayloadCapture(
        user_id=user_id,
        current_era_payload=sample_payload,
        current_era_min_message_seq=10,
        current_era_captured_at=now,
        current_era_request_id="req-current",
        current_era_payload_bytes=128,
    )
    if with_response:
        row.current_era_response = {
            "schema_version": 1,
            "content_blocks": [{"type": "text", "text": "model reply"}],
            "stop_reason": "end_turn",
        }
        row.current_era_response_captured_at = now
        row.current_era_response_bytes = 96
    if with_previous:
        prev_payload = {**sample_payload, "messages": [{"role": "user", "content": "old"}]}
        row.previous_era_payload = prev_payload
        row.previous_era_min_message_seq = 3
        row.previous_era_captured_at = now - datetime.timedelta(minutes=10)
        row.previous_era_request_id = "req-previous"
        row.previous_era_payload_bytes = 100
        if with_response:
            row.previous_era_response = {
                "schema_version": 1,
                "content_blocks": [{"type": "text", "text": "older reply"}],
                "stop_reason": "end_turn",
            }
            row.previous_era_response_captured_at = now - datetime.timedelta(minutes=10)
            row.previous_era_response_bytes = 96
    db_session.add(row)
    db_session.commit()


def test_returns_404_when_no_capture_row(
    client: TestClient,
    db_session: Session,
    test_user: User,
    test_subscription: Subscription,
) -> None:
    target_id = _insert_target_user(db_session, consent=True)

    resp = client.get(f"/api/admin/users/{target_id}/llm-payloads")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "No captured payloads for this user"


def test_returns_404_for_unknown_user(
    client: TestClient,
    test_user: User,
    test_subscription: Subscription,
) -> None:
    resp = client.get("/api/admin/users/00000000-0000-0000-0000-000000000000/llm-payloads")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "User not found"


def test_returns_payload_with_both_eras(
    client: TestClient,
    db_session: Session,
    test_user: User,
    test_subscription: Subscription,
) -> None:
    target_id = _insert_target_user(db_session, consent=True)
    _insert_capture(db_session, user_id=target_id, with_previous=True)

    resp = client.get(f"/api/admin/users/{target_id}/llm-payloads")

    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == target_id
    assert "exported_at" in body
    # Top-level latest_capture_at mirrors current_era.captured_at for
    # at-a-glance freshness in admin tooling.
    assert body["latest_capture_at"] == body["current_era"]["captured_at"]
    assert body["current_era"]["request_id"] == "req-current"
    assert body["current_era"]["min_message_seq"] == 10
    assert body["current_era"]["payload"]["model"] == "claude-test"
    assert body["previous_era"] is not None
    assert body["previous_era"]["request_id"] == "req-previous"
    assert body["previous_era"]["min_message_seq"] == 3
    # Download header is set so admin frontend can save the file directly.
    cd = resp.headers.get("content-disposition", "")
    assert "attachment" in cd
    assert f"llm-payloads-{target_id}.json" in cd


def test_returns_payload_with_only_current_era(
    client: TestClient,
    db_session: Session,
    test_user: User,
    test_subscription: Subscription,
) -> None:
    target_id = _insert_target_user(db_session, consent=True)
    _insert_capture(db_session, user_id=target_id, with_previous=False)

    resp = client.get(f"/api/admin/users/{target_id}/llm-payloads")

    assert resp.status_code == 200
    body = resp.json()
    assert body["current_era"]["request_id"] == "req-current"
    assert body["previous_era"] is None


def test_returns_404_and_purges_row_when_consent_revoked(
    client: TestClient,
    db_session: Session,
    test_user: User,
    test_subscription: Subscription,
) -> None:
    """Defense-in-depth: a user who revoked consent after capture must
    not see their payloads exfiltrated, and the row should be purged on
    the read attempt."""
    target_id = _insert_target_user(db_session, consent=False)
    _insert_capture(db_session, user_id=target_id, with_previous=True)
    assert (
        db_session.query(LLMPayloadCapture).filter_by(user_id=target_id).one_or_none() is not None
    )

    resp = client.get(f"/api/admin/users/{target_id}/llm-payloads")

    assert resp.status_code == 404
    db_session.expire_all()
    assert db_session.query(LLMPayloadCapture).filter_by(user_id=target_id).one_or_none() is None


def test_returns_response_data_when_paired(
    client: TestClient,
    db_session: Session,
    test_user: User,
    test_subscription: Subscription,
) -> None:
    """When the request capture has a paired response, the admin export
    must surface ``response`` / ``response_captured_at`` / ``response_bytes``
    per era so forensic analysis can see what the model actually emitted.

    Existing tests pass because the response columns default to NULL,
    but a regression that drops the response fields from the response
    body would have gone unnoticed without this positive assertion.
    """
    target_id = _insert_target_user(db_session, consent=True)
    _insert_capture(db_session, user_id=target_id, with_previous=True, with_response=True)

    resp = client.get(f"/api/admin/users/{target_id}/llm-payloads")

    assert resp.status_code == 200
    body = resp.json()
    current = body["current_era"]
    assert current["response"]["content_blocks"][0]["text"] == "model reply"
    assert current["response"]["stop_reason"] == "end_turn"
    assert current["response_captured_at"] is not None
    assert current["response_bytes"] == 96

    previous = body["previous_era"]
    assert previous is not None
    assert previous["response"]["content_blocks"][0]["text"] == "older reply"
    assert previous["response_captured_at"] is not None
    assert previous["response_bytes"] == 96


def test_response_fields_null_when_unpaired(
    client: TestClient,
    db_session: Session,
    test_user: User,
    test_subscription: Subscription,
) -> None:
    """Existing-row backward compat: a capture row written before the
    response observer landed (or for a turn where the response was
    dropped) renders the response fields as null without breaking the
    export."""
    target_id = _insert_target_user(db_session, consent=True)
    _insert_capture(db_session, user_id=target_id, with_previous=False, with_response=False)

    resp = client.get(f"/api/admin/users/{target_id}/llm-payloads")

    assert resp.status_code == 200
    body = resp.json()
    current = body["current_era"]
    assert current["response"] is None
    assert current["response_captured_at"] is None
    assert current["response_bytes"] is None
