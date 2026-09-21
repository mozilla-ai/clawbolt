"""Sign-in, JWT sessions, and per-tenant usage quotas (multi_user only)."""

from __future__ import annotations

from pydantic import BaseModel


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class StateResponse(BaseModel):
    state: str


class GoogleAuthRequest(BaseModel):
    code: str
    state: str


class AuthResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user_id: str


class UsageBucket(BaseModel):
    used: int
    limit: int


class UsageSummary(BaseModel):
    messages: UsageBucket
    tokens: UsageBucket
    period_start: str | None


class AdminUsageSummary(UsageSummary):
    """Admin-only variant that adds aggregate LLM spend.

    Kept separate from the user-facing ``UsageSummary`` because we do
    not want to surface raw API cost back to end users via
    ``/account/usage``: the app is free to them, and the dollar
    figure is operational data for Mozilla.ai. Costs are formatted
    decimal strings (``"82.553261"``) to match the per-row
    ``LLMUsageLogItem.cost_usd`` shape and avoid float precision loss.
    """

    period_cost_usd: str
    lifetime_cost_usd: str
