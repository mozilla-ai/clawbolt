"""Endpoints for contractor profile management."""

from fastapi import APIRouter, Depends, HTTPException

from backend.app.agent.file_store import ContractorData, get_contractor_store
from backend.app.auth.dependencies import get_current_user
from backend.app.schemas import ContractorProfileResponse, ContractorProfileUpdate

router = APIRouter()


def _profile_response(c: ContractorData) -> ContractorProfileResponse:
    return ContractorProfileResponse(
        id=c.id,
        user_id=c.user_id,
        name=c.name,
        phone=c.phone,
        trade=c.trade,
        location=c.location,
        hourly_rate=c.hourly_rate,
        business_hours=c.business_hours,
        timezone=c.timezone,
        assistant_name=c.assistant_name,
        soul_text=c.soul_text,
        preferred_channel=c.preferred_channel,
        channel_identifier=c.channel_identifier,
        heartbeat_opt_in=c.heartbeat_opt_in,
        heartbeat_frequency=c.heartbeat_frequency,
        onboarding_complete=c.onboarding_complete,
        is_active=c.is_active,
        created_at=c.created_at.isoformat(),
        updated_at=c.updated_at.isoformat(),
    )


@router.get("/contractor/profile", response_model=ContractorProfileResponse)
async def get_profile(
    current_user: ContractorData = Depends(get_current_user),
) -> ContractorProfileResponse:
    """Return the current contractor's profile."""
    return _profile_response(current_user)


@router.put("/contractor/profile", response_model=ContractorProfileResponse)
async def update_profile(
    body: ContractorProfileUpdate,
    current_user: ContractorData = Depends(get_current_user),
) -> ContractorProfileResponse:
    """Partial update of the current contractor's profile."""
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    store = get_contractor_store()
    updated = await store.update(current_user.id, **updates)
    if updated is None:
        raise HTTPException(status_code=404, detail="Contractor not found")

    return _profile_response(updated)
