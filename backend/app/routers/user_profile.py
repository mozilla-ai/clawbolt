"""Endpoints for user profile management."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.app.auth.dependencies import get_current_user
from backend.app.config import save_persistent_config, settings, update_settings
from backend.app.database import get_db
from backend.app.models import User
from backend.app.schemas import (
    ChannelConfigResponse,
    ChannelConfigUpdate,
    UserProfileResponse,
    UserProfileUpdate,
)

router = APIRouter()


def _profile_response(c: User) -> UserProfileResponse:
    return UserProfileResponse(
        id=c.id,
        user_id=c.user_id,
        phone=c.phone,
        timezone=c.timezone,
        soul_text=c.soul_text,
        user_text=c.user_text,
        heartbeat_text=c.heartbeat_text,
        preferred_channel=c.preferred_channel,
        channel_identifier=c.channel_identifier,
        heartbeat_opt_in=c.heartbeat_opt_in,
        heartbeat_frequency=c.heartbeat_frequency,
        llm_model=c.llm_model,
        llm_provider=c.llm_provider,
        onboarding_complete=c.onboarding_complete,
        is_active=c.is_active,
        created_at=c.created_at.isoformat(),
        updated_at=c.updated_at.isoformat(),
    )


@router.get("/user/profile", response_model=UserProfileResponse)
async def get_profile(
    current_user: User = Depends(get_current_user),
) -> UserProfileResponse:
    """Return the current user's profile."""
    return _profile_response(current_user)


@router.put("/user/profile", response_model=UserProfileResponse)
async def update_profile(
    body: UserProfileUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserProfileResponse:
    """Partial update of the current user's profile."""
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    # Re-query user in the current session to avoid detached instance issues
    user = db.query(User).filter_by(id=current_user.id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    for key, value in updates.items():
        setattr(user, key, value)
    db.commit()
    db.refresh(user)
    return _profile_response(user)


# ---------------------------------------------------------------------------
# Channel config
# ---------------------------------------------------------------------------


def _build_channel_config_response() -> ChannelConfigResponse:
    return ChannelConfigResponse(
        telegram_bot_token_set=bool(settings.telegram_bot_token),
        telegram_allowed_usernames=settings.telegram_allowed_usernames,
    )


@router.get("/user/channels/config", response_model=ChannelConfigResponse)
async def get_channel_config(
    _current_user: User = Depends(get_current_user),
) -> ChannelConfigResponse:
    """Return server-level channel configuration."""
    return _build_channel_config_response()


@router.put("/user/channels/config", response_model=ChannelConfigResponse)
async def update_channel_config(
    body: ChannelConfigUpdate,
    _current_user: User = Depends(get_current_user),
) -> ChannelConfigResponse:
    """Update server-level channel configuration."""
    updates = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    try:
        update_settings(updates)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Persist to config.json inside the volume-mounted data directory.
    save_persistent_config(updates)

    # If the bot token changed, reset the live TelegramChannel instance.
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
