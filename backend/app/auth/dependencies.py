from backend.app.agent.file_store import ContractorData, get_contractor_store

LOCAL_USER_ID = "local@clawbolt.local"


async def get_current_user() -> ContractorData:
    """OSS mode: return the single contractor, no auth required.

    In single-tenant mode there should be exactly one contractor. If Telegram
    (or another channel) already created one, return that contractor so the
    dashboard sees the same sessions, memory, and stats. Only create a local
    fallback when the store is completely empty.
    """
    store = get_contractor_store()
    all_contractors = await store.list_all()
    if all_contractors:
        return all_contractors[0]
    return await store.create(
        user_id=LOCAL_USER_ID,
        name="Local Contractor",
    )
