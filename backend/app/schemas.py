import datetime
from typing import Any

from pydantic import BaseModel, Field

from backend.app.enums import ChecklistSchedule, EstimateStatus


class HealthResponse(BaseModel):
    status: str


class UserBase(BaseModel):
    name: str = ""
    phone: str = ""


class UserCreate(UserBase):
    user_id: str


class UserResponse(UserBase):
    id: int
    user_id: str
    created_at: datetime.datetime
    updated_at: datetime.datetime


class MemoryBase(BaseModel):
    key: str
    value: str
    category: str = "general"


class MemoryCreate(MemoryBase):
    confidence: float = 1.0


class MemoryResponse(MemoryBase):
    confidence: float
    user_id: int


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
    name: str
    phone: str
    timezone: str
    assistant_name: str
    soul_text: str
    user_text: str
    preferred_channel: str
    channel_identifier: str
    heartbeat_opt_in: bool
    heartbeat_frequency: str
    onboarding_complete: bool
    is_active: bool
    created_at: str
    updated_at: str


class UserProfileUpdate(BaseModel):
    name: str | None = None
    phone: str | None = None
    timezone: str | None = None
    assistant_name: str | None = None
    soul_text: str | None = None
    user_text: str | None = None
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
    messages: list[SessionMessage]


# ---------------------------------------------------------------------------
# Memory / facts (dashboard)
# ---------------------------------------------------------------------------


class MemoryFactResponse(BaseModel):
    key: str
    value: str
    category: str
    confidence: float


class MemoryFactUpdate(BaseModel):
    value: str | None = None
    category: str | None = None
    confidence: float | None = None


# ---------------------------------------------------------------------------
# Checklist (dashboard)
# ---------------------------------------------------------------------------


class ChecklistCreateRequest(BaseModel):
    description: str = Field(..., min_length=1)
    schedule: str = ChecklistSchedule.DAILY


class ChecklistUpdateRequest(BaseModel):
    description: str | None = None
    schedule: str | None = None
    status: str | None = None


class ChecklistItemResponse(BaseModel):
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
    active_checklist_items: int
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
    enabled: bool


class ToolConfigResponse(BaseModel):
    tools: list[ToolConfigEntryResponse]


class ToolConfigUpdateEntry(BaseModel):
    name: str
    enabled: bool


class ToolConfigUpdate(BaseModel):
    tools: list[ToolConfigUpdateEntry]
