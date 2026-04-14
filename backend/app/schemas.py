from typing import Any

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str
    database: str = "ok"


class MemoryResponse(BaseModel):
    content: str


class MemoryUpdate(BaseModel):
    content: str


class MessageBase(BaseModel):
    direction: str
    body: str = ""


class MessageResponse(MessageBase):
    seq: int
    timestamp: str


# ---------------------------------------------------------------------------
# User profile (dashboard)
# ---------------------------------------------------------------------------


class UserProfileResponse(BaseModel):
    id: str
    user_id: str
    phone: str
    timezone: str
    soul_text: str
    user_text: str
    heartbeat_text: str
    preferred_channel: str
    channel_identifier: str
    heartbeat_opt_in: bool
    heartbeat_frequency: str
    heartbeat_max_daily: int = 0
    onboarding_complete: bool
    is_active: bool
    created_at: str
    updated_at: str


class UserProfileUpdate(BaseModel):
    phone: str | None = None
    timezone: str | None = None
    soul_text: str | None = None
    user_text: str | None = None
    heartbeat_text: str | None = None
    heartbeat_opt_in: bool | None = None
    heartbeat_frequency: str | None = None
    heartbeat_max_daily: int | None = Field(default=None, ge=0)
    onboarding_complete: bool | None = None


# ---------------------------------------------------------------------------
# Conversation sessions (dashboard)
# ---------------------------------------------------------------------------


class SessionMessage(BaseModel):
    seq: int
    direction: str
    body: str = ""
    timestamp: str
    tool_interactions: list[dict[str, Any]] = Field(default_factory=list)


class SessionDetailResponse(BaseModel):
    session_id: str
    user_id: str
    created_at: str
    last_message_at: str
    is_active: bool
    channel: str = ""
    initial_system_prompt: str = ""
    last_compacted_seq: int = 0
    messages: list[SessionMessage]


# ---------------------------------------------------------------------------
# Channel config (dashboard)
# ---------------------------------------------------------------------------


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


class ChannelRouteResponse(BaseModel):
    channel: str
    channel_identifier: str
    enabled: bool
    created_at: str


class ChannelRouteListResponse(BaseModel):
    routes: list[ChannelRouteResponse]


class ChannelRouteUpdate(BaseModel):
    enabled: bool


class TelegramBotInfoResponse(BaseModel):
    bot_username: str
    bot_link: str


# ---------------------------------------------------------------------------
# Provider info (used by admin panel for dynamic provider listing)
# ---------------------------------------------------------------------------


class ProviderInfo(BaseModel):
    name: str
    local: bool


# ---------------------------------------------------------------------------
# Model config (dashboard)
# ---------------------------------------------------------------------------


class ModelConfigResponse(BaseModel):
    llm_provider: str
    llm_model: str
    llm_api_base: str | None
    vision_model: str
    vision_provider: str
    heartbeat_model: str
    heartbeat_provider: str
    compaction_model: str
    compaction_provider: str
    reasoning_effort: str


class ModelConfigUpdate(BaseModel):
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_api_base: str | None = None
    vision_model: str | None = None
    vision_provider: str | None = None
    heartbeat_model: str | None = None
    heartbeat_provider: str | None = None
    compaction_model: str | None = None
    compaction_provider: str | None = None
    reasoning_effort: str | None = None


# ---------------------------------------------------------------------------
# Storage config (dashboard)
# ---------------------------------------------------------------------------


class StorageConfigResponse(BaseModel):
    storage_provider: str
    file_storage_base_dir: str
    dropbox_access_token_set: bool
    google_drive_credentials_json_set: bool


class StorageConfigUpdate(BaseModel):
    storage_provider: str | None = None
    file_storage_base_dir: str | None = None
    dropbox_access_token: str | None = None
    google_drive_credentials_json: str | None = None


# ---------------------------------------------------------------------------
# Tool config (dashboard)
# ---------------------------------------------------------------------------


class SubToolEntryResponse(BaseModel):
    name: str
    description: str
    enabled: bool
    permission_level: str = "always"
    hidden_in_permissions: bool = False


class ToolConfigEntryResponse(BaseModel):
    name: str
    description: str
    category: str
    domain_group: str = ""
    domain_group_order: int = 0
    enabled: bool
    configured: bool = True
    auth_message: str = ""
    sub_tools: list[SubToolEntryResponse] = Field(default_factory=list)


class ToolConfigResponse(BaseModel):
    tools: list[ToolConfigEntryResponse]


class ToolConfigUpdateEntry(BaseModel):
    name: str
    enabled: bool
    disabled_sub_tools: list[str] | None = None


class ToolConfigUpdate(BaseModel):
    tools: list[ToolConfigUpdateEntry]


# ---------------------------------------------------------------------------
# Heartbeat logs (admin)
# ---------------------------------------------------------------------------


class HeartbeatLogItemResponse(BaseModel):
    id: int
    user_id: str
    action_type: str = "send"
    message_text: str = ""
    channel: str = ""
    reasoning: str = ""
    tasks: str = ""
    created_at: str


class HeartbeatLogListResponse(BaseModel):
    total: int
    items: list[HeartbeatLogItemResponse]


class DeleteHeartbeatLogsResponse(BaseModel):
    status: str
    deleted: int


# ---------------------------------------------------------------------------
# Session list (admin)
# ---------------------------------------------------------------------------


class SessionListItem(BaseModel):
    session_id: str
    channel: str = ""
    is_active: bool
    message_count: int
    created_at: str
    last_message_at: str


class SessionListResponse(BaseModel):
    total: int
    items: list[SessionListItem]


class DeleteMessagesResponse(BaseModel):
    status: str
    messages_deleted: int


class BatchDeleteRequest(BaseModel):
    seqs: list[int] = Field(..., min_length=1, max_length=1000)


class DeleteMessageResponse(BaseModel):
    status: str
    seq: int


# ---------------------------------------------------------------------------
# LLM usage summary (admin)
# ---------------------------------------------------------------------------


class LLMUsageByPurpose(BaseModel):
    purpose: str
    call_count: int
    total_input_tokens: int
    total_output_tokens: int
    total_tokens: int
    total_cost: float


class LLMUsageSummary(BaseModel):
    total_calls: int
    total_tokens: int
    total_cost: float
    by_purpose: list[LLMUsageByPurpose]


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------


class OAuthStatusEntry(BaseModel):
    integration: str
    configured: bool
    connected: bool


class OAuthStatusResponse(BaseModel):
    integrations: list[OAuthStatusEntry]


class OAuthAuthorizeResponse(BaseModel):
    url: str
    integration: str


# ---------------------------------------------------------------------------
# Calendar config
# ---------------------------------------------------------------------------


class CalendarListEntry(BaseModel):
    id: str
    summary: str
    primary: bool = False
    access_role: str = ""


class CalendarListResponse(BaseModel):
    calendars: list[CalendarListEntry]


class CalendarConfigEntry(BaseModel):
    calendar_id: str
    display_name: str
    disabled_tools: list[str] = Field(default_factory=list)
    access_role: str = ""


class CalendarConfigResponse(BaseModel):
    calendars: list[CalendarConfigEntry]


class CalendarConfigUpdate(BaseModel):
    calendars: list[CalendarConfigEntry]


# ---------------------------------------------------------------------------
# Permissions (PERMISSIONS.json via API)
# ---------------------------------------------------------------------------


class PermissionsResponse(BaseModel):
    content: str


class PermissionsUpdate(BaseModel):
    content: str
