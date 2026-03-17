from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from backend.app.database import get_db
from backend.app.models import User


async def get_scoped_user(
    current_user: User,
    target_id: str,
    db: Session = Depends(get_db),
) -> User:
    """Get a user by ID, scoped to the current user. Returns 404 on mismatch."""
    target = db.query(User).filter(User.id == str(target_id)).first()
    if not target or target.user_id != current_user.user_id:
        raise HTTPException(status_code=404, detail="User not found")
    return target
