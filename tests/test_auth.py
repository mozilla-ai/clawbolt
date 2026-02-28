from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.app.auth.dependencies import LOCAL_USER_ID, _get_or_create_local_contractor
from backend.app.auth.scoping import get_user_contractor
from backend.app.models import Contractor


def test_get_current_user_creates_local_contractor(db_session: Session) -> None:
    """OSS mode should auto-create a local contractor."""
    contractor = _get_or_create_local_contractor(db_session)
    assert contractor.user_id == LOCAL_USER_ID
    assert contractor.name == "Local Contractor"
    assert contractor.id is not None


def test_get_current_user_returns_same_contractor(db_session: Session) -> None:
    """Calling twice should return the same contractor."""
    c1 = _get_or_create_local_contractor(db_session)
    c2 = _get_or_create_local_contractor(db_session)
    assert c1.id == c2.id


def test_auth_config_returns_none_mode(client: TestClient) -> None:
    """OSS mode should return method=none."""
    response = client.get("/api/auth/config")
    assert response.status_code == 200
    data = response.json()
    assert data == {"method": "none", "required": False}


def test_scoping_returns_404_for_wrong_user(db_session: Session) -> None:
    """Scoping should return 404 when contractor doesn't belong to user."""
    # Create two contractors with different user_ids
    contractor1 = Contractor(user_id="user-1", name="Contractor 1")
    contractor2 = Contractor(user_id="user-2", name="Contractor 2")
    db_session.add_all([contractor1, contractor2])
    db_session.commit()
    db_session.refresh(contractor1)
    db_session.refresh(contractor2)

    # User 1 should not be able to access contractor 2
    import pytest
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        get_user_contractor(db_session, contractor1, contractor2.id)
    assert exc_info.value.status_code == 404


def test_scoping_returns_contractor_for_correct_user(db_session: Session) -> None:
    """Scoping should return contractor when user_id matches."""
    contractor = Contractor(user_id="user-1", name="My Contractor")
    db_session.add(contractor)
    db_session.commit()
    db_session.refresh(contractor)

    result = get_user_contractor(db_session, contractor, contractor.id)
    assert result.id == contractor.id
