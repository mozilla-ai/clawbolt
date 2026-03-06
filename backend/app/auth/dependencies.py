from backend.app.agent.file_store import ContractorData, get_contractor_store

LOCAL_USER_ID = "local@clawbolt.local"


async def get_current_user() -> ContractorData:
    """OSS mode: return the single local contractor, no auth required."""
    store = get_contractor_store()
    contractor = await store.get_by_user_id(LOCAL_USER_ID)
    if contractor is None:
        contractor = await store.create(
            user_id=LOCAL_USER_ID,
            name="Local Contractor",
        )
    return contractor
