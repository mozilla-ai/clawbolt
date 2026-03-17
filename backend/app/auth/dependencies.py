from fastapi import Depends
from sqlalchemy.orm import Session

from backend.app.agent.user_db import provision_user_directory
from backend.app.database import get_db
from backend.app.models import User

LOCAL_USER_ID = "local@clawbolt.local"


async def get_current_user(db: Session = Depends(get_db)) -> User:
    """OSS mode: return the single user, no auth required.

    In single-tenant mode there should be exactly one user. If Telegram
    (or another channel) already created one, return that user so the
    dashboard sees the same sessions, memory, and stats. Only create a local
    fallback when the store is completely empty.
    """
    user = db.query(User).first()
    if user:
        return user
    user = User(user_id=LOCAL_USER_ID)
    db.add(user)
    db.commit()
    db.refresh(user)
    provision_user_directory(user)
    return user
