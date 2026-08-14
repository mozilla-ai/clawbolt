"""Track user login timestamps.

Updates the ``last_login_at`` column on the OSS User table and clears
any pending ``inactivity_warned_at`` flag so the warning timer resets.
"""

import datetime
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def update_last_login(db: AsyncSession, user_id: str) -> None:
    """Set last_login_at to now and clear inactivity_warned_at.

    Uses raw SQL to avoid loading the full User ORM object.

    ``last_login_at`` is TIMESTAMP WITHOUT TIME ZONE (see migration p009);
    asyncpg refuses tz-aware datetimes for that column, so strip tzinfo
    before binding (the value is already UTC).
    """
    now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    await db.execute(
        text(
            "UPDATE users SET last_login_at = :now, inactivity_warned_at = NULL WHERE id = :user_id"
        ),
        {"now": now, "user_id": user_id},
    )
    await db.flush()
