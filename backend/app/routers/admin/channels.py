"""Admin console: server-level channel settings and Telegram webhook registration."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from backend.app.channels import is_bluebubbles_configured, reset_channel_clients
from backend.app.config import settings, update_settings
from backend.app.config_store import get_settings_store, strip_unchanged_secrets
from backend.app.schemas.channels import (
    AdminChannelConfigResponse,
    AdminChannelConfigUpdate,
    TelegramWebhookRequest,
    TelegramWebhookResponse,
)
from backend.app.services.admin_audit import (
    AdminAction,
    AdminAuditContext,
    audit_admin,
)
from backend.app.services.telegram_webhook import register_webhook, unregister_webhook

router = APIRouter()


def _build_admin_channel_config() -> AdminChannelConfigResponse:
    return AdminChannelConfigResponse(
        bluebubbles_server_url=settings.bluebubbles_server_url,
        bluebubbles_password_set=bool(settings.bluebubbles_password),
        bluebubbles_imessage_address=settings.bluebubbles_imessage_address,
        bluebubbles_send_method=settings.bluebubbles_send_method,
        bluebubbles_configured=is_bluebubbles_configured(),
        telegram_bot_token_set=bool(settings.telegram_bot_token),
        telegram_allowed_chat_id=settings.telegram_allowed_chat_id,
        linq_api_token_set=bool(settings.linq_api_token),
        linq_from_number=settings.linq_from_number,
        linq_allowed_numbers=settings.linq_allowed_numbers,
        linq_preferred_service=settings.linq_preferred_service,
        twilio_account_sid_set=bool(settings.twilio_account_sid),
        twilio_auth_token_set=bool(settings.twilio_auth_token),
        twilio_api_key_sid_set=bool(settings.twilio_api_key_sid),
        twilio_api_key_secret_set=bool(settings.twilio_api_key_secret),
        twilio_configured=bool(
            settings.twilio_account_sid
            and settings.twilio_auth_token
            and settings.twilio_api_key_sid
            and settings.twilio_api_key_secret
        ),
        twilio_phone_number=settings.twilio_phone_number,
        twilio_messaging_service_sid=settings.twilio_messaging_service_sid,
        twilio_allowed_numbers=settings.twilio_allowed_numbers,
    )


@router.get("/channels/config", response_model=AdminChannelConfigResponse)
async def get_admin_channel_config(
    _ctx: AdminAuditContext = Depends(audit_admin(AdminAction.VIEW_CHANNEL_CONFIG)),
) -> AdminChannelConfigResponse:
    """Return full server-level channel configuration for the admin panel."""
    return _build_admin_channel_config()


@router.put("/channels/config", response_model=AdminChannelConfigResponse)
async def update_admin_channel_config(
    body: AdminChannelConfigUpdate,
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.UPDATE_CHANNEL_CONFIG)),
) -> AdminChannelConfigResponse:
    """Update server-level channel configuration (admin only)."""
    raw_updates = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    if not raw_updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    # Strip ``MASK`` round-trips for unchanged secret fields (the UI
    # re-submits ``********`` for any secret the admin didn't retype).
    updates = strip_unchanged_secrets(raw_updates)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    # Record only the keys updated; values may include secrets so never log them.
    ctx.resource_type = "channel_config"
    ctx.detail = {"keys": sorted(updates.keys())}

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

    await get_settings_store().save(updates, actor_user_id=ctx.admin_user_id)
    reset_channel_clients(updates)

    return _build_admin_channel_config()


@router.post("/telegram/webhook", response_model=TelegramWebhookResponse)
async def register_telegram_webhook_endpoint(
    body: TelegramWebhookRequest,
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.SET_TELEGRAM_WEBHOOK)),
) -> TelegramWebhookResponse:
    """Register or update the Telegram webhook URL.

    If webhook_url is empty, constructs it from APP_BASE_URL.
    """
    ctx.resource_type = "telegram_webhook"
    ok, url = await register_webhook(body.webhook_url)
    if not ok:
        raise HTTPException(status_code=502, detail="Failed to register Telegram webhook")
    return TelegramWebhookResponse(status="registered", webhook_url=url)


@router.delete("/telegram/webhook", response_model=TelegramWebhookResponse)
async def unregister_telegram_webhook_endpoint(
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.DELETE_TELEGRAM_WEBHOOK)),
) -> TelegramWebhookResponse:
    """Remove the Telegram webhook."""
    ctx.resource_type = "telegram_webhook"
    ok = await unregister_webhook()
    if not ok:
        raise HTTPException(status_code=502, detail="Failed to unregister Telegram webhook")
    return TelegramWebhookResponse(status="unregistered", webhook_url="")
