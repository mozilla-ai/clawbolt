from backend.app.agent.file_store import UserData, get_user_store

LOCAL_USER_ID = "local@clawbolt.local"


async def get_current_user() -> UserData:
    """OSS mode: return the single user, no auth required.

    In single-tenant mode there should be exactly one user. If Telegram
    (or another channel) already created one, return that user so the
    dashboard sees the same sessions, memory, and stats. Only create a local
    fallback when the store is completely empty.
    """
    store = get_user_store()
    all_users = await store.list_all()
    if all_users:
        return all_users[0]
    return await store.create(
        user_id=LOCAL_USER_ID,
    )
