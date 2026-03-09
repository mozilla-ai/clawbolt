from fastapi import HTTPException

from backend.app.agent.file_store import (
    ClientStore,
    EstimateStore,
    UserData,
    get_memory_store,
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


async def get_user_client(
    user: UserData,
    client_id: str,
) -> None:
    """Verify a client exists and belongs to the current user. 404 on mismatch."""
    client_store = ClientStore(user.id)
    client = await client_store.get(client_id)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")


async def get_user_estimate(
    user: UserData,
    estimate_id: str,
) -> None:
    """Verify an estimate exists and belongs to the current user. 404 on mismatch."""
    estimate_store = EstimateStore(user.id)
    estimate = await estimate_store.get(estimate_id)
    if not estimate:
        raise HTTPException(status_code=404, detail="Estimate not found")


async def get_user_memory(
    user: UserData,
    memory_key: str,
) -> None:
    """Verify a memory fact exists for the current user. 404 on mismatch."""
    memory_store = get_memory_store(user.id)
    memories = await memory_store.get_all_memories()
    for m in memories:
        if m.key == memory_key:
            return
    raise HTTPException(status_code=404, detail="Memory not found")
