"""Endpoints for user profile management."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from backend.app.auth.dependencies import get_current_user
from backend.app.config import save_persistent_config, settings, update_settings
from backend.app.database import get_db
from backend.app.models import User
from backend.app.schemas import (
    ChannelConfigResponse,
    ChannelConfigUpdate,
    ModelConfigResponse,
    ModelConfigUpdate,
    ProviderInfo,
    StorageConfigResponse,
    StorageConfigUpdate,
    UserProfileResponse,
    UserProfileUpdate,
)
from backend.app.services.llm_service import get_configured_providers, get_models

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
        telegram_allowed_chat_id=settings.telegram_allowed_chat_id,
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

    # Enforce single Telegram chat ID (no comma-separated lists).
    chat_id = updates.get("telegram_allowed_chat_id", "")
    if chat_id and chat_id != "*" and "," in chat_id:
        raise HTTPException(
            status_code=422,
            detail="Only a single Telegram user ID is allowed. Remove commas.",
        )

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


# ---------------------------------------------------------------------------
# Model config
# ---------------------------------------------------------------------------


def _build_model_config_response() -> ModelConfigResponse:
    return ModelConfigResponse(
        llm_provider=settings.llm_provider,
        llm_model=settings.llm_model,
        llm_api_base=settings.llm_api_base,
        vision_model=settings.vision_model,
        vision_provider=settings.vision_provider,
        heartbeat_model=settings.heartbeat_model,
        heartbeat_provider=settings.heartbeat_provider,
        compaction_model=settings.compaction_model,
        compaction_provider=settings.compaction_provider,
        reasoning_effort=settings.reasoning_effort,
    )


@router.get("/user/model/config", response_model=ModelConfigResponse)
async def get_model_config(
    _current_user: User = Depends(get_current_user),
) -> ModelConfigResponse:
    """Return server-level LLM model configuration."""
    return _build_model_config_response()


@router.put("/user/model/config", response_model=ModelConfigResponse)
async def update_model_config(
    body: ModelConfigUpdate,
    _current_user: User = Depends(get_current_user),
) -> ModelConfigResponse:
    """Update server-level LLM model configuration."""
    updates = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    try:
        update_settings(updates)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    save_persistent_config(updates)
    return _build_model_config_response()


# ---------------------------------------------------------------------------
# Storage config
# ---------------------------------------------------------------------------

_VALID_STORAGE_PROVIDERS = {"local", "dropbox", "google_drive"}


def _build_storage_config_response() -> StorageConfigResponse:
    return StorageConfigResponse(
        storage_provider=settings.storage_provider,
        file_storage_base_dir=settings.file_storage_base_dir,
        dropbox_access_token_set=bool(settings.dropbox_access_token),
        google_drive_credentials_json_set=bool(settings.google_drive_credentials_json),
    )


@router.get("/user/storage/config", response_model=StorageConfigResponse)
async def get_storage_config(
    _current_user: User = Depends(get_current_user),
) -> StorageConfigResponse:
    """Return server-level storage configuration."""
    return _build_storage_config_response()


@router.put("/user/storage/config", response_model=StorageConfigResponse)
async def update_storage_config(
    body: StorageConfigUpdate,
    _current_user: User = Depends(get_current_user),
) -> StorageConfigResponse:
    """Update server-level storage configuration."""
    updates = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    if (
        "storage_provider" in updates
        and updates["storage_provider"] not in _VALID_STORAGE_PROVIDERS
    ):
        raise HTTPException(
            status_code=422,
            detail=f"Invalid storage_provider: must be one of {sorted(_VALID_STORAGE_PROVIDERS)}",
        )

    try:
        update_settings(updates)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    save_persistent_config(updates)
    return _build_storage_config_response()


# ---------------------------------------------------------------------------
# Provider / model enumeration
# ---------------------------------------------------------------------------


@router.get("/user/providers", response_model=list[ProviderInfo])
async def list_providers(
    _current_user: User = Depends(get_current_user),
) -> list[ProviderInfo]:
    """List available LLM providers from any-llm."""
    return get_configured_providers()


@router.get("/user/providers/{provider}/models")
async def list_provider_models(
    provider: str,
    api_base: str | None = Query(None),
    _current_user: User = Depends(get_current_user),
) -> list[str]:
    """List available models for a provider."""
    try:
        return await get_models(provider, api_base=api_base)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to list models: {exc}") from exc
