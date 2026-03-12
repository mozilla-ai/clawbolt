from typing import Any

from pydantic import BaseModel, Field

from backend.app.enums import EstimateStatus, HeartbeatSchedule


class HealthResponse(BaseModel):
    status: str


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


class EstimateLineItemBase(BaseModel):
    description: str = ""
    quantity: float = 1.0
    unit_price: float = 0.0
    total: float = 0.0


class EstimateBase(BaseModel):
    description: str = ""
    total_amount: float = 0.0
    status: str = EstimateStatus.DRAFT


class EstimateResponse(EstimateBase):
    id: str
    user_id: int
    client_id: str | None = None
    pdf_url: str = ""
    storage_path: str = ""
    created_at: str


# ---------------------------------------------------------------------------
# User profile (dashboard)
# ---------------------------------------------------------------------------


class UserProfileResponse(BaseModel):
    id: int
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
    user_id: int
    created_at: str
    last_message_at: str
    is_active: bool
    channel: str = ""
    messages: list[SessionMessage]


# ---------------------------------------------------------------------------
# Heartbeat (dashboard)
# ---------------------------------------------------------------------------


class HeartbeatCreateRequest(BaseModel):
    description: str = Field(..., min_length=1)
    schedule: str = HeartbeatSchedule.DAILY


class HeartbeatUpdateRequest(BaseModel):
    description: str | None = None
    schedule: str | None = None
    status: str | None = None


class HeartbeatItemResponse(BaseModel):
    id: int
    description: str
    schedule: str
    status: str
    created_at: str


# ---------------------------------------------------------------------------
# Overview stats (dashboard)
# ---------------------------------------------------------------------------


class UserStatsResponse(BaseModel):
    total_sessions: int
    messages_this_month: int
    active_heartbeat_items: int
    total_memory_facts: int
    last_conversation_at: str | None


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
