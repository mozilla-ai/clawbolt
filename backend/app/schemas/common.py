"""Primitives shared across every API surface."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

# The reasoning effort levels the product understands, in ascending order.
# ``services.llm_service.REASONING_EFFORT_VALUES`` derives its tuple from this,
# so those two cannot drift from each other. It is deliberately a subset of
# any-llm's own ``ReasoningEffort``, which also accepts ``"max"``: adding a
# level here means deciding what budget it maps to in ``_EFFORT_TO_BUDGET``.
ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "auto"]


class HealthResponse(BaseModel):
    status: str
    database: str = "ok"


class AppConfigResponse(BaseModel):
    """Deployment-level feature flags the frontend reads on app load."""

    chat_web_attachments_enabled: bool


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


class StatusResponse(BaseModel):
    status: str
