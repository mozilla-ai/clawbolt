"""Heartbeat log listings, for the dashboard and the admin console."""

from __future__ import annotations

from pydantic import BaseModel


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


class AdminHeartbeatLogItem(BaseModel):
    """Heartbeat log metadata only.

    Content fields (``message_text``, ``reasoning``, ``tasks``) were
    removed in #325 work item 2. They were user-facing content the
    user-detail response also stripped. They surface only via the
    consent-gated paths once items 3 + 4 land.
    """

    id: int
    user_id: str
    action_type: str = "send"
    channel: str = ""
    created_at: str


class AdminHeartbeatLogListResponse(BaseModel):
    total: int
    items: list[AdminHeartbeatLogItem]
