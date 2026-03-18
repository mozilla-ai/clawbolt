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


# ---------------------------------------------------------------------------
# Conversation sessions (dashboard)
# ---------------------------------------------------------------------------


class SessionSummary(BaseModel):
    id: str
    start_time: str
    message_count: int
    last_message_preview: str = ""
    channel: str = ""


class SessionListResponse(BaseModel):
    sessions: list[SessionSummary]
    total: int
    offset: int
    limit: int


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
    messages: list[SessionMessage]


# ---------------------------------------------------------------------------
# Channel config (dashboard)
# ---------------------------------------------------------------------------


class ChannelConfigResponse(BaseModel):
    telegram_bot_token_set: bool
    telegram_allowed_usernames: str


class ChannelConfigUpdate(BaseModel):
    telegram_bot_token: str | None = None
    telegram_allowed_usernames: str | None = None


# ---------------------------------------------------------------------------
# Model config (dashboard)
# ---------------------------------------------------------------------------


class ModelConfigResponse(BaseModel):
    llm_provider: str
    llm_model: str
    llm_api_base: str | None
    vision_model: str
    heartbeat_model: str
    heartbeat_provider: str
    compaction_model: str
    compaction_provider: str


class ModelConfigUpdate(BaseModel):
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_api_base: str | None = None
    vision_model: str | None = None
    heartbeat_model: str | None = None
    heartbeat_provider: str | None = None
    compaction_model: str | None = None
    compaction_provider: str | None = None


# ---------------------------------------------------------------------------
# Tool config (dashboard)
# ---------------------------------------------------------------------------


class ToolConfigEntryResponse(BaseModel):
    name: str
    description: str
    category: str
    domain_group: str = ""
    domain_group_order: int = 0
    enabled: bool


class ToolConfigResponse(BaseModel):
    tools: list[ToolConfigEntryResponse]


class ToolConfigUpdateEntry(BaseModel):
    name: str
    enabled: bool


class ToolConfigUpdate(BaseModel):
    tools: list[ToolConfigUpdateEntry]


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
