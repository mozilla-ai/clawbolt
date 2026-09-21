"""Channel configuration and per-channel identity linking."""

from __future__ import annotations

from pydantic import BaseModel


class ChannelConfigResponse(BaseModel):
    telegram_bot_token_set: bool
    telegram_allowed_chat_id: str
    linq_api_token_set: bool = False
    linq_from_number: str = ""
    linq_allowed_numbers: str = ""
    linq_preferred_service: str = "iMessage"
    bluebubbles_configured: bool = False
    bluebubbles_allowed_numbers: str = ""
    bluebubbles_imessage_address: str = ""
    # Resolved iMessage backend ("linq" | "bluebubbles" | None).
    # The UI uses this to render a single iMessage card without exposing which
    # backend powers it; None means iMessage is not configured on this server.
    imessage_backend: str | None = None
    # Twilio (RCS via Messaging Service, with SMS/MMS fallback).
    # ``twilio_configured`` requires the account SID plus the Standard API
    # key pair used for REST calls; the auth token alone is not enough
    # because outbound message creation now runs through API-key Basic
    # auth. The auth token is still required separately for inbound
    # webhook signature validation, but it is not part of the
    # "ready to send" check.
    twilio_configured: bool = False
    twilio_phone_number: str = ""
    twilio_messaging_service_sid: str = ""
    twilio_allowed_numbers: str = ""


class ChannelConfigUpdate(BaseModel):
    telegram_bot_token: str | None = None
    telegram_allowed_chat_id: str | None = None
    linq_api_token: str | None = None
    linq_from_number: str | None = None
    linq_webhook_signing_secret: str | None = None
    linq_allowed_numbers: str | None = None
    linq_preferred_service: str | None = None
    bluebubbles_server_url: str | None = None
    bluebubbles_password: str | None = None
    bluebubbles_allowed_numbers: str | None = None
    bluebubbles_imessage_address: str | None = None
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_api_key_sid: str | None = None
    twilio_api_key_secret: str | None = None
    twilio_phone_number: str | None = None
    twilio_messaging_service_sid: str | None = None
    twilio_allowed_numbers: str | None = None


class ChannelRouteResponse(BaseModel):
    channel: str
    channel_identifier: str
    enabled: bool
    created_at: str
    # ISO-8601 timestamp of the last inbound message that resolved to this
    # route, or None if the user has never successfully messaged through it.
    # The channel picker UI uses this to flip to a "Verified" state.
    last_inbound_at: str | None = None


class ChannelRouteListResponse(BaseModel):
    routes: list[ChannelRouteResponse]


class ChannelRouteUpdate(BaseModel):
    enabled: bool


class TelegramBotInfoResponse(BaseModel):
    bot_username: str
    bot_link: str


class TelegramLinkRequest(BaseModel):
    telegram_user_id: str


class TelegramLinkResponse(BaseModel):
    telegram_user_id: str | None
    connected: bool


class LinqLinkRequest(BaseModel):
    phone_number: str  # E.164 format, e.g. "+15551234567"


class LinqLinkResponse(BaseModel):
    phone_number: str | None
    connected: bool
    linq_from_number: str = ""


class BlueBubblesLinkRequest(BaseModel):
    phone_number: str  # E.164 phone or iCloud email


class BlueBubblesLinkResponse(BaseModel):
    phone_number: str | None = None
    connected: bool = False


class TwilioLinkRequest(BaseModel):
    phone_number: str


class TwilioLinkResponse(BaseModel):
    phone_number: str | None = None
    connected: bool = False


class WelcomeTextResponse(BaseModel):
    """Result of POST /api/channels/{channel}/welcome.

    ``sent`` is True when the channel's ``send_text`` returned without raising.
    ``channel_identifier`` echoes the destination so the UI can render
    "we texted +1555..." without a second round trip.
    """

    sent: bool
    channel: str
    channel_identifier: str


class AdminChannelConfigResponse(BaseModel):
    bluebubbles_server_url: str = ""
    bluebubbles_password_set: bool = False
    bluebubbles_imessage_address: str = ""
    bluebubbles_send_method: str = "apple-script"
    bluebubbles_configured: bool = False
    telegram_bot_token_set: bool = False
    telegram_allowed_chat_id: str = ""
    linq_api_token_set: bool = False
    linq_from_number: str = ""
    linq_allowed_numbers: str = ""
    linq_preferred_service: str = "iMessage"
    # Twilio (RCS via Messaging Service, with SMS/MMS fallback).
    # ``twilio_configured`` requires the account SID plus the API key
    # pair used for REST calls. The auth token is retained only for
    # inbound webhook signature validation. ``messaging_service_sid``
    # is the canonical outbound sender (RCS-capable, SMS fallback);
    # ``phone_number`` is the SMS-only operator fallback.
    twilio_account_sid_set: bool = False
    twilio_auth_token_set: bool = False
    twilio_api_key_sid_set: bool = False
    twilio_api_key_secret_set: bool = False
    twilio_configured: bool = False
    twilio_phone_number: str = ""
    twilio_messaging_service_sid: str = ""
    twilio_allowed_numbers: str = ""


class AdminChannelConfigUpdate(BaseModel):
    bluebubbles_server_url: str | None = None
    bluebubbles_password: str | None = None
    bluebubbles_imessage_address: str | None = None
    bluebubbles_send_method: str | None = None
    telegram_bot_token: str | None = None
    telegram_allowed_chat_id: str | None = None
    linq_api_token: str | None = None
    linq_from_number: str | None = None
    linq_allowed_numbers: str | None = None
    linq_preferred_service: str | None = None
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_api_key_sid: str | None = None
    twilio_api_key_secret: str | None = None
    twilio_phone_number: str | None = None
    twilio_messaging_service_sid: str | None = None
    twilio_allowed_numbers: str | None = None


class TelegramWebhookRequest(BaseModel):
    webhook_url: str = ""


class TelegramWebhookResponse(BaseModel):
    status: str
    webhook_url: str
