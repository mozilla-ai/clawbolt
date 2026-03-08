"""Endpoints for contractor profile management."""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from backend.app.agent.file_store import ContractorData, get_contractor_store
from backend.app.auth.dependencies import get_current_user
from backend.app.config import settings
from backend.app.schemas import (
    ChannelConfigResponse,
    ChannelConfigUpdate,
    ContractorProfileResponse,
    ContractorProfileUpdate,
)

router = APIRouter()


def _profile_response(c: ContractorData) -> ContractorProfileResponse:
    return ContractorProfileResponse(
        id=c.id,
        user_id=c.user_id,
        name=c.name,
        phone=c.phone,
        timezone=c.timezone,
        assistant_name=c.assistant_name,
        soul_text=c.soul_text,
        user_text=c.user_text,
        preferred_channel=c.preferred_channel,
        channel_identifier=c.channel_identifier,
        heartbeat_opt_in=c.heartbeat_opt_in,
        heartbeat_frequency=c.heartbeat_frequency,
        onboarding_complete=c.onboarding_complete,
        is_active=c.is_active,
        created_at=c.created_at.isoformat(),
        updated_at=c.updated_at.isoformat(),
    )


@router.get("/user/profile", response_model=ContractorProfileResponse)
async def get_profile(
    current_user: ContractorData = Depends(get_current_user),
) -> ContractorProfileResponse:
    """Return the current contractor's profile."""
    return _profile_response(current_user)


@router.put("/user/profile", response_model=ContractorProfileResponse)
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


# ---------------------------------------------------------------------------
# Channel config
# ---------------------------------------------------------------------------

_ENV_KEY_MAP: dict[str, str] = {
    "telegram_bot_token": "TELEGRAM_BOT_TOKEN",
    "telegram_allowed_usernames": "TELEGRAM_ALLOWED_USERNAMES",
}


def _build_channel_config_response() -> ChannelConfigResponse:
    return ChannelConfigResponse(
        telegram_bot_token_set=bool(settings.telegram_bot_token),
        telegram_allowed_usernames=settings.telegram_allowed_usernames,
    )


@router.get("/user/channels/config", response_model=ChannelConfigResponse)
async def get_channel_config(
    _current_user: ContractorData = Depends(get_current_user),
) -> ChannelConfigResponse:
    """Return server-level channel configuration."""
    return _build_channel_config_response()


@router.put("/user/channels/config", response_model=ChannelConfigResponse)
async def update_channel_config(
    body: ChannelConfigUpdate,
    _current_user: ContractorData = Depends(get_current_user),
) -> ChannelConfigResponse:
    """Update server-level channel configuration."""
    updates = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    env_path = Path(".env")
    env_exists = env_path.is_file()

    for field, value in updates.items():
        # Update the in-memory settings singleton
        setattr(settings, field, value)

        # Persist to .env if it exists
        if env_exists:
            from dotenv import set_key

            set_key(str(env_path), _ENV_KEY_MAP[field], value)

    # If the bot token changed, reset the live TelegramChannel instance
    if "telegram_bot_token" in updates:
        try:
            from backend.app.channels import get_channel
            from backend.app.channels.telegram import TelegramChannel

            channel = get_channel("telegram")
            if isinstance(channel, TelegramChannel):
                channel._token = settings.telegram_bot_token
                channel._bot = None
        except KeyError:
            pass

    return _build_channel_config_response()
