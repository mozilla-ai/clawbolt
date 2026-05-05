from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.agent.user_db import provision_user
from backend.app.database import get_async_db
from backend.app.models import User

LOCAL_USER_ID = "local@clawbolt.local"


async def get_current_user(db: AsyncSession = Depends(get_async_db)) -> User:
    """OSS mode: return the single user, no auth required.

    In single-tenant mode there should be exactly one user. If Telegram
    (or another channel) already created one, return that user so the
    dashboard sees the same sessions, memory, and stats. Only create a local
    fallback when the store is completely empty.
    """
    user = (await db.execute(select(User))).scalars().first()
    if user:
        return user
    user = User(user_id=LOCAL_USER_ID)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    # ``provision_user`` is the sync seed-and-bootstrap path. The user row
    # is committed above so it is visible to a fresh sync session; passing
    # ``db=None`` lets ``provision_user`` open its own ``SessionLocal()``
    # rather than trying to share the AsyncSession.
    provision_user(user)
    return user
