"""User profile, conversation sessions, permissions, and the account page."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


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
    data_sharing_consent: bool = False
    data_sharing_consent_at: str | None = None
    created_at: str
    updated_at: str


class UserProfileUpdate(BaseModel):
    """Fields the client is allowed to update on the current user.

    ``onboarding_complete`` is deliberately not writable here. It is owned by
    the backend (set by ``OnboardingSubscriber`` when the LLM deletes
    BOOTSTRAP.md or heuristic evidence appears) so the conversational
    onboarding can't be short-circuited by the UI.

    ``data_sharing_consent`` is deliberately not writable here either:
    it has its own dedicated endpoint (``PUT /api/user/data-sharing-consent``)
    that always stamps ``data_sharing_consent_at``. Routing it through
    this generic patch endpoint would lose the timestamp guarantee.

    ``model_config`` pins ``extra="ignore"`` so unknown fields (including
    ``data_sharing_consent`` if a client tries to slip it through here)
    are silently dropped. This is the contract the dedicated-endpoint
    test relies on. If pydantic ever flips the global default to
    ``"forbid"``, this declaration keeps the contract stable.
    """

    model_config = {"extra": "ignore"}

    phone: str | None = None
    timezone: str | None = None
    soul_text: str | None = None
    user_text: str | None = None
    heartbeat_text: str | None = None
    heartbeat_opt_in: bool | None = None
    heartbeat_frequency: str | None = None
    heartbeat_max_daily: int | None = Field(default=None, ge=0)


class DataSharingConsentRequest(BaseModel):
    """Body for ``PUT /api/user/data-sharing-consent``.

    Single boolean. The endpoint always stamps ``data_sharing_consent_at``
    with ``now()`` regardless of whether ``consent`` is ``True`` or
    ``False``, so consent toggle history can be reconstructed even when
    no separate audit table exists.
    """

    consent: bool


class DataSharingConsentResponse(BaseModel):
    """Returned by the consent setter and getter.

    ``data_sharing_consent_at`` is the timestamp of the last toggle,
    not "first opted in." If a user opts in, then opts out, this column
    holds the opt-out time.
    """

    data_sharing_consent: bool
    data_sharing_consent_at: str | None


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
    channel: str = ""
    messages: list[SessionMessage]


class SessionSystemPromptResponse(BaseModel):
    """Live system prompt that would be sent to the LLM on the next turn.

    Reconstructed on demand from current user state (memory, profile,
    onboarding status, available tools). The historical first-turn
    snapshot lives on the ``ChatSession.initial_system_prompt`` column
    for forensics but is intentionally not exposed via the public API,
    since it reveals the operator's preamble and tool wiring. Premium
    deployments additionally gate this endpoint behind an admin guard.
    """

    session_id: str
    system_prompt: str
    is_onboarding: bool


class PermissionsResponse(BaseModel):
    content: str


class PermissionsUpdate(BaseModel):
    content: str


class ProfileResponse(BaseModel):
    id: str
    plan: str
    role: str
