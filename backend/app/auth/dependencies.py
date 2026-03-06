import asyncio

from backend.app.agent.file_store import ContractorData, get_contractor_store

LOCAL_USER_ID = "local@clawbolt.local"


async def _get_or_create_local_contractor() -> ContractorData:
    store = get_contractor_store()
    contractor = await store.get_by_user_id(LOCAL_USER_ID)
    if contractor is None:
        contractor = await store.create(
            user_id=LOCAL_USER_ID,
            name="Local Contractor",
        )
    return contractor


def get_current_user() -> ContractorData:
    """OSS mode: return the single local contractor, no auth required."""
    return asyncio.get_event_loop().run_until_complete(_get_or_create_local_contractor())
