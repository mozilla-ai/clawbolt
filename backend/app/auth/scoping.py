from fastapi import HTTPException

from backend.app.agent.file_store import (
    UserData,
    get_user_store,
)


async def get_scoped_user(
    current_user: UserData,
    target_id: int,
) -> UserData:
    """Get a user by ID, scoped to the current user. Returns 404 on mismatch."""
    store = get_user_store()
    target = await store.get_by_id(target_id)
    if not target or target.user_id != current_user.user_id:
        raise HTTPException(status_code=404, detail="User not found")
    return target
