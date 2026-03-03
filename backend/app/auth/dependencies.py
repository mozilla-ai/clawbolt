from fastapi import Depends
from sqlalchemy.orm import Session

from backend.app.database import get_db
from backend.app.models import Contractor

LOCAL_USER_ID = "local@clawbolt.local"


def _get_or_create_local_contractor(db: Session) -> Contractor:
    contractor = db.query(Contractor).filter(Contractor.user_id == LOCAL_USER_ID).first()
    if contractor is None:
        contractor = Contractor(
            user_id=LOCAL_USER_ID,
            name="Local Contractor",
            phone="",
            trade="",
        )
        db.add(contractor)
        db.commit()
        db.refresh(contractor)
    return contractor


def get_current_user(db: Session = Depends(get_db)) -> Contractor:
    """OSS mode: return the single local contractor, no auth required."""
    return _get_or_create_local_contractor(db)
