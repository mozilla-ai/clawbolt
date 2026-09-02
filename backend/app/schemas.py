from typing import Any

from pydantic import BaseModel, Field, SecretStr


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
# Tool config (dashboard)
# ---------------------------------------------------------------------------


class SubToolEntryResponse(BaseModel):
    name: str
    description: str
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
    # Name of the OAuth integration backing this tool (as registered in
    # ``backend.app.services.oauth``), or empty when the tool is not
    # OAuth-backed. Lets the Settings UI render Connect/Disconnect buttons
    # without hand-maintaining a factory-to-OAuth map per integration.
    oauth_name: str = ""
    # When ``True``, the backend refuses to disable this tool (mirrors
    # ``ToolFactory.dashboard_always_enabled``). The Settings UI uses this
    # to hide the enable/disable toggle so the user does not see a switch
    # that silently bounces back. Decoupled from ``category`` so future
    # purely-internal categories cannot accidentally hide the toggle for
    # always-on OAuth tools (Google Drive).
    always_enabled: bool = False
    # Integration key for tools that connect by submitting secrets through a
    # web form (ServiceTitan client credentials, AppFolio magic link) rather
    # than an OAuth redirect. Empty when the tool is not form-connected. The
    # Settings UI keys off this to render the right credential form instead of
    # an OAuth Connect button, so these secrets never travel through a chat
    # thread (issue #1337).
    connect_form: str = ""
    sub_tools: list[SubToolEntryResponse] = Field(default_factory=list)


class ToolConfigResponse(BaseModel):
    tools: list[ToolConfigEntryResponse]


class SubToolPermissionUpdate(BaseModel):
    """Per-sub-tool permission override sent by the Settings UI.

    ``permission_level`` is the new value: ``"always"`` (auto-run),
    ``"ask"`` (prompt before running), or ``"never"`` (hide from the
    LLM schema). Sub-tools omitted from the update list keep their
    current stored level.
    """

    name: str
    permission_level: str


class ToolConfigUpdateEntry(BaseModel):
    name: str
    enabled: bool
    sub_tools: list[SubToolPermissionUpdate] | None = None


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
# Web-form integration connections (ServiceTitan, AppFolio Vendor Portal)
#
# These integrations authenticate with pasted secrets rather than an OAuth
# redirect. The web app collects the secrets over an authenticated HTTPS
# session and submits them here, so they never land in a chat thread
# (issue #1337).
# ---------------------------------------------------------------------------


class ServiceTitanConnectRequest(BaseModel):
    """The three values from ServiceTitan's API Application Access page.

    ``client_secret`` is a ``SecretStr`` so it is masked in logs/reprs and
    marked write-only in the OpenAPI schema; the value still arrives as a
    plain JSON string from the client.
    """

    tenant_id: str = Field(..., min_length=1)
    client_id: str = Field(..., min_length=1)
    client_secret: SecretStr = Field(..., min_length=1)


class AppFolioConnectRequest(BaseModel):
    """A pasted AppFolio magic link (full URL or the bare token).

    ``magic_link`` is a single-use secret, so it is a ``SecretStr`` (masked
    in logs/reprs, write-only in the OpenAPI schema).
    """

    magic_link: SecretStr = Field(..., min_length=1)


class IntegrationConnectionResponse(BaseModel):
    """Result of a connect/disconnect on a web-form integration."""

    integration: str
    connected: bool


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


# ---------------------------------------------------------------------------
# Multi-user deployment schemas
#
# Request/response models for the endpoints that only mount under
# ``AUTH_MODE=multi_user``: OAuth login, the account page, the admin
# console, and the waitlist.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Common
# ---------------------------------------------------------------------------


class StatusResponse(BaseModel):
    status: str


# ---------------------------------------------------------------------------
# Auth / OAuth
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------


class ProfileResponse(BaseModel):
    id: str
    plan: str
    role: str


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Admin: users
# ---------------------------------------------------------------------------


class UserItem(BaseModel):
    id: str
    user_id: str
    email: str
    plan: str
    status: str
    role: str
    is_active: bool
    onboarding_complete: bool
    created_at: str | None = None
    last_login_at: str | None = None
    last_message_at: str | None = None
    messages_this_month: int = 0
    # Research data sharing. ``data_sharing_consent`` is the user's
    # current opt-in state (the column on the OSS users table); admins
    # use it as a filter to find pilot users. ``data_sharing_consent_at``
    # is the most recent toggle time, opt-in OR opt-out, so the UI can
    # show "opted in 3 days ago" / "withdrew 2 hours ago" without a
    # second round trip. ``conversation_count`` is the per-user session
    # total surfaced so admins can pick the user with the most signal
    # to review without first drilling into the shared-data tab.
    data_sharing_consent: bool = False
    data_sharing_consent_at: str | None = None
    conversation_count: int = 0


class UserListResponse(BaseModel):
    total: int
    offset: int
    limit: int
    items: list[UserItem]


class UserActiveResponse(BaseModel):
    id: str
    is_active: bool


class AdminToolConfigEntry(BaseModel):
    tool_name: str
    enabled: bool


class AdminChannelRouteEntry(BaseModel):
    channel: str
    channel_identifier: str
    enabled: bool
    last_inbound_at: str | None


class AdminUserPermissionEntry(BaseModel):
    """One tool-level permission override (always, ask, or deny)."""

    tool_name: str
    level: str


class AdminUserResourcePermissionEntry(BaseModel):
    """One resource-scoped permission override.

    ``resource`` may be a literal value or a glob pattern such as ``*.gov``,
    matching the OSS approval store's resolution order.
    """

    tool_name: str
    resource: str
    level: str


class AdminUserPermissions(BaseModel):
    """Per-user tool/resource permission overrides.

    Mirrors the JSON document at ``user_permissions.data``: the tool list
    is the top-level approval level for each tool, and resources are the
    finer-grained overrides keyed by (tool, resource pattern).
    """

    tools: list[AdminUserPermissionEntry]
    resources: list[AdminUserResourcePermissionEntry]


class AdminUserDetailResponse(BaseModel):
    """Identity, account state, and configuration metadata for one user.

    User-authored content (memory, soul, user text, heartbeat directives,
    message bodies, tool-call args/results) was removed in #325 work
    item 2. The plan: content surfaces only via the consent-gated paths
    (``/admin/reported-conversations`` and ``/admin/shared-data``) once
    items 3 + 4 land. Until then, admins debugging an incident see the
    metadata + integrations here, plus the audit log of who looked.

    Channel routes carry a *masked* ``channel_identifier``. Phone
    numbers / iMessage emails / Telegram chat IDs are PII the admin
    rarely needs in full. The route applies ``_mask_channel_identifier``
    so admins see enough to recognize a route and confirm last-4 digits,
    not enough to dial / message directly from the admin panel.
    """

    id: str
    user_id: str
    email: str
    plan: str
    status: str
    role: str
    is_active: bool
    onboarding_complete: bool
    subscription_created_at: str | None
    subscription_updated_at: str | None
    # Profile config (not content)
    timezone: str
    preferred_channel: str
    heartbeat_opt_in: bool
    heartbeat_frequency: str
    # Integrations / configuration
    tool_configs: list[AdminToolConfigEntry]
    channel_routes: list[AdminChannelRouteEntry]
    # Per-user tool / resource approval levels (OSS approval store).
    permissions: AdminUserPermissions


# ---------------------------------------------------------------------------
# Admin: compact-now (clear poisoned context without dropping memory)
# ---------------------------------------------------------------------------


class CompactUserContextRequest(BaseModel):
    """Body for ``POST /admin/users/{user_id}/compact-now``.

    Both fields are optional: a bare ``{}`` runs the default "compact
    everything visible, no LLM steering" behavior.
    """

    keep_recent: int = Field(
        default=0,
        ge=0,
        description=(
            "Preserve the last N visible messages from compaction so the "
            "agent retains immediate context (e.g. a pending user request)."
        ),
    )
    hint: str | None = Field(
        default=None,
        description=(
            "Optional steering note prepended inside the compaction LLM's "
            "<conversation> block as `[admin note: ...]`. Use to bias how "
            "the LLM reads the conversation, e.g. 'ignore prior agent "
            "self-claims about AppFolio capabilities'."
        ),
    )


class CompactUserContextResponse(BaseModel):
    """Outcome of an admin-triggered context compaction.

    ``event_id`` is the row this call wrote, populated only when the
    call did real work. ``previous_event_id`` is populated only on
    no-op returns and points at the most recent prior compaction event
    for the user (if any), so admin tooling can tell apart "you already
    did this seconds ago" from "there was never anything to do".
    """

    compacted_message_count: int
    new_watermark: int | None
    memory_updated: bool
    event_id: int | None
    previous_event_id: int | None = None


# ---------------------------------------------------------------------------
# Admin: shared
# ---------------------------------------------------------------------------


class DeleteResponse(BaseModel):
    deleted: bool
    id: int


# ---------------------------------------------------------------------------
# Admin: allowed emails
# ---------------------------------------------------------------------------


class AllowedEmailCreate(BaseModel):
    email: str
    note: str = ""


class AllowedEmailResponse(BaseModel):
    id: int
    email: str
    note: str
    created_at: str


class AllowedEmailListResponse(BaseModel):
    total: int
    items: list[AllowedEmailResponse]


# ---------------------------------------------------------------------------
# Waitlist
# ---------------------------------------------------------------------------


class WaitlistJoinRequest(BaseModel):
    email: str
    name: str = ""
    use_case: str = ""
    source: str = "homepage"


class WaitlistEntryResponse(BaseModel):
    id: int
    email: str
    name: str
    use_case: str | None = None
    source: str
    created_at: str


class WaitlistListResponse(BaseModel):
    total: int
    items: list[WaitlistEntryResponse]


# ---------------------------------------------------------------------------
# Channels / Telegram linking
# ---------------------------------------------------------------------------


class TelegramLinkRequest(BaseModel):
    telegram_user_id: str


class TelegramLinkResponse(BaseModel):
    telegram_user_id: str | None
    connected: bool


# ---------------------------------------------------------------------------
# Channels / Linq (iMessage/RCS/SMS) linking
# ---------------------------------------------------------------------------


class LinqLinkRequest(BaseModel):
    phone_number: str  # E.164 format, e.g. "+15551234567"


class LinqLinkResponse(BaseModel):
    phone_number: str | None
    connected: bool
    linq_from_number: str = ""


# ---------------------------------------------------------------------------
# Channels / BlueBubbles (iMessage via self-hosted Mac bridge) linking
# ---------------------------------------------------------------------------


class BlueBubblesLinkRequest(BaseModel):
    phone_number: str  # E.164 phone or iCloud email


class BlueBubblesLinkResponse(BaseModel):
    phone_number: str | None = None
    connected: bool = False


# ---------------------------------------------------------------------------
# Channels / Twilio (RCS via Messaging Service, with SMS/MMS fallback)
# ---------------------------------------------------------------------------


class TwilioLinkRequest(BaseModel):
    phone_number: str


class TwilioLinkResponse(BaseModel):
    phone_number: str | None = None
    connected: bool = False


# ---------------------------------------------------------------------------
# Channels / welcome-text (onboarding kickoff)
# ---------------------------------------------------------------------------


class WelcomeTextResponse(BaseModel):
    """Result of POST /api/channels/{channel}/welcome.

    ``sent`` is True when the channel's ``send_text`` returned without raising.
    ``channel_identifier`` echoes the destination so the UI can render
    "we texted +1555..." without a second round trip.
    """

    sent: bool
    channel: str
    channel_identifier: str


# ---------------------------------------------------------------------------
# Admin: channel config
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Admin: LLM config (global default + per-user overrides)
# ---------------------------------------------------------------------------


class AdminLLMConfigResponse(BaseModel):
    """Global default LLM (used when a user has no per-user override)."""

    llm_provider: str
    llm_model: str
    llm_api_base: str | None = None


class AdminLLMConfigUpdate(BaseModel):
    """All fields optional. Pass only what you want to change."""

    llm_provider: str | None = None
    llm_model: str | None = None
    llm_api_base: str | None = None


class AdminUserLLMOverrideResponse(BaseModel):
    """Per-user override values plus the resolved (effective) values.

    Empty ``llm_provider_override`` / ``llm_model_override`` mean "fall
    back to the global default". The effective fields show what the
    agent will actually use, factoring in fallbacks.
    """

    user_id: str
    llm_provider_override: str
    llm_model_override: str
    effective_llm_provider: str
    effective_llm_model: str


class AdminUserLLMOverrideUpdate(BaseModel):
    """Pass empty strings to clear an override and fall back to the global default."""

    llm_provider_override: str | None = None
    llm_model_override: str | None = None


class AdminUserPlanUpdate(BaseModel):
    """Set a user's subscription plan. Must be a key in ``billing.plans.PLANS``."""

    plan: str


class AdminUserPlanResponse(BaseModel):
    """Resulting plan plus the active month's caps after the change."""

    user_id: str
    plan: str
    messages_limit: int
    tokens_limit: int


class AdminLLMProvider(BaseModel):
    """One entry in the admin provider list."""

    name: str
    local: bool  # True for providers like ollama/llamafile that need no API key


class AdminLLMProvidersResponse(BaseModel):
    providers: list[AdminLLMProvider]


class AdminLLMModelsResponse(BaseModel):
    """Structured result of an ``alist_models`` call, with failure context.

    The admin UI uses this to decide between:
      - rendering a real ``<select>`` (``models`` non-empty)
      - rendering an inline error + text-input fallback (``error`` set)
      - rendering a "this provider does not support listing models"
        notice + text-input fallback (``supports_listing == False``)
    """

    provider: str
    models: list[str]
    supports_listing: bool
    error: str | None = None


# ---------------------------------------------------------------------------
# Admin: heartbeat logs
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Admin: LLM usage logs
# ---------------------------------------------------------------------------


class LLMUsageLogItem(BaseModel):
    id: int
    timestamp: str
    provider: str
    model: str
    purpose: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost_usd: str
    cache_creation_input_tokens: int | None
    cache_read_input_tokens: int | None


class LLMUsageLogListResponse(BaseModel):
    total: int
    items: list[LLMUsageLogItem]


# ---------------------------------------------------------------------------
# Admin: media staging diagnostics
# ---------------------------------------------------------------------------


class StagedMediaItem(BaseModel):
    """One row of ``staged_media`` for the admin diagnostics view.

    Excludes ``disk_path`` (an internal artifact) and the bytes
    themselves (not consent-gated content but no reason to ship MB of
    image data through an admin list endpoint).
    """

    handle: str
    original_url: str
    mime_type: str
    created_at: str
    expires_at: str
    upload_service: str | None
    upload_status: str | None
    uploaded_at: str | None


class StagedMediaListResponse(BaseModel):
    total: int
    active: int
    uploaded: int
    cap: int
    items: list[StagedMediaItem]


# ---------------------------------------------------------------------------
# Admin: inbound webhook events
# ---------------------------------------------------------------------------


class WebhookEventItem(BaseModel):
    """One ``idempotency_keys`` row, optionally joined to the message it created.

    ``message_persisted`` is the join result: True if a Message row
    exists for this ``external_id``, False if the webhook was accepted
    (allowlist + dedup passed) but no Message landed (consumed by the
    approval gate, channel disabled, or consumer-side failure).
    """

    external_id: str
    created_at: str
    message_persisted: bool
    user_id: str | None
    message_timestamp: str | None
    media_count: int


class WebhookEventListResponse(BaseModel):
    total: int
    items: list[WebhookEventItem]


# ---------------------------------------------------------------------------
# Admin: stats
# ---------------------------------------------------------------------------


class AdminStatsResponse(BaseModel):
    telegram_configured: bool = False
    bluebubbles_configured: bool = False
    twilio_configured: bool = False


class AdminVersionResponse(BaseModel):
    """Build metadata for the admin overview's version card and auto-reload poll.

    ``started_at`` is the load-bearing field: a fresh process picks up a new
    timestamp, which lets the admin client detect a deploy without depending
    on commit env vars being stamped at build time.
    """

    premium_version: str
    premium_commit: str
    oss_version: str
    oss_commit: str
    started_at: str


# ---------------------------------------------------------------------------
# Admin: shared-data (consent-gated content access, issue #325 item 3)
# ---------------------------------------------------------------------------


class SharedDataUserItem(BaseModel):
    """One consenting user in the shared-data list."""

    id: str
    user_id: str
    email: str
    consent_at: str | None
    conversation_count: int
    last_message_at: str | None


class SharedDataUserListResponse(BaseModel):
    total: int
    items: list[SharedDataUserItem]


class SharedDataTopUserItem(BaseModel):
    """One row in the "top consenting users this week" leaderboard.

    Surfaced on the Overview pilot panel: small enough to render
    inline without a drill-down, useful enough to answer "who is
    actually using the assistant in our pilot right now?".
    """

    id: str
    email: str
    user_id: str
    messages_this_week: int


class SharedDataSummaryResponse(BaseModel):
    """Aggregate counts across the consenting-user pilot population.

    Used by the Overview "Research pilot" panel to render at-a-glance
    metrics that previously required visiting the Shared, Reported,
    and per-user Activity tabs separately.

    Fields are deliberately conservative: only counts and a small
    leaderboard, never message bodies or memory. The bodies stay
    behind the existing per-conversation endpoints, which already
    PII-redact and audit-log every read.

    ``consents_changed_this_week`` counts every user currently
    consenting whose ``data_sharing_consent_at`` toggled within the
    last 7 days. The OSS column ticks on every change (opt-in OR
    opt-out), so the count surfaces "consent state moved recently"
    rather than "first-time opt-ins". A user who toggles off and back
    on within the week still counts.

    A heartbeat-error metric was deliberately excluded: the OSS
    heartbeat scheduler writes ``action_type`` of ``send | skip |
    cleanup`` and never ``error``, so any "errors this week" count
    we built here would always be zero. If we want a real error
    signal in the future, it has to come from a different source
    (LLMUsageLog failures, the Reported queue, structured logs).
    """

    consenting_user_count: int
    consents_changed_this_week: int
    conversations_this_week: int
    heartbeats_this_week: int
    open_reports_count: int
    top_users_this_week: list[SharedDataTopUserItem]


class SharedDataConversationItem(BaseModel):
    """The consenting user's conversation.

    ``last_trim_seq`` is the highest ``messages.seq`` that the agent's
    trim path has dropped from live LLM context. Messages with
    ``seq <= last_trim_seq`` are still in the database (and still ship
    in the turns endpoint) but the agent no longer sees them on the
    next inbound. ``None`` means nothing has been trimmed yet on this
    session, including legacy sessions that predate the watermark.
    Surfacing it here lets the admin UI render a "trimmed by
    compaction" line in the timeline at the boundary between dropped
    and live messages.
    """

    session_id: str
    channel: str
    created_at: str | None
    last_message_at: str | None
    message_count: int
    last_trim_seq: int | None = None


class SharedDataMessageItem(BaseModel):
    """One message inside a consenting user's conversation, PII-redacted.

    ``body`` has been passed through :func:`pii_redaction.redact_pii`
    before serialization. The original plaintext is never returned by
    this endpoint. ``thinking`` carries the LLM's extended-thinking
    output for outbound messages (see OSS migration 033); it is empty
    for inbound messages and for outbound rows persisted before the
    capture path was wired up. ``thinking`` runs through the same
    shape-based redaction as ``body`` (emails, phones, cards, tokens
    masked by regex). Names and other free-form identifiers are not
    masked because the redactor has no shape to match them against,
    same caveat as ``body``.
    """

    seq: int
    direction: str
    body: str
    thinking: str = ""
    timestamp: str | None


# ---------------------------------------------------------------------------
# Admin: turn-grouped conversation inspector (design 2026-05-01)
# ---------------------------------------------------------------------------
#
# Turns nest ``SharedDataMessageItem`` for the user message and agent reply.


class SharedDataReceipt(BaseModel):
    """Tool receipt redacted for admin display.

    Mirrors ``StoredToolReceipt`` from ``backend/app/agent/context.py``
    but each string field is passed through :func:`pii_redaction.redact_pii`
    before serialization.
    """

    action: str = ""
    target: str = ""
    url: str | None = None


class SharedDataToolCall(BaseModel):
    """One tool call inside a turn, redacted at the leaves.

    ``args`` and ``result`` are redacted via :func:`pii_redaction.redact_pii_recursive`
    so customer names, phone numbers, tokens, etc. that the agent passed
    to a tool or got back from one do not surface verbatim to admins.
    """

    tool_call_id: str
    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    result: str = ""
    is_error: bool = False
    receipt: SharedDataReceipt | None = None


class SharedDataTurn(BaseModel):
    """One conversational turn: inbound user message + agent reply(ies) + tool calls.

    A turn starts at an inbound message and includes every outbound
    message until the next inbound (or end of conversation). Tool calls
    aggregate from every outbound message in the turn, in order. A turn
    can also have no ``user_message`` at all when the agent initiated
    it (heartbeat tick, scheduled action).
    """

    turn_index: int
    user_message: SharedDataMessageItem | None = None
    agent_reply: SharedDataMessageItem | None = None
    tool_calls: list[SharedDataToolCall] = Field(default_factory=list)
    started_at: str | None = None
    finished_at: str | None = None


class SharedDataConversationTurnsResponse(BaseModel):
    session_id: str
    user_id: str
    consent_at: str | None
    turns: list[SharedDataTurn]
    total: int
    last_trim_seq: int | None = None


# ---------------------------------------------------------------------------
# Admin: shared-data profile / heartbeat logs / memory (consent-gated, redacted)
# ---------------------------------------------------------------------------


class SharedDataProfileResponse(BaseModel):
    """User profile + agent personality text for one consenting user.

    All three text fields go through ``redact_pii`` before serialization
    so phone numbers / emails / tokens that the user pasted into their
    soul or memory directives don't surface verbatim. ``soul_text``
    (agent personality), ``user_text`` (synthesized profile), and
    ``heartbeat_text`` (proactive directives) used to live on
    ``GET /admin/users/{id}`` until the slimming PR (#336); this is the
    consent-gated home for them.
    """

    user_id: str
    consent_at: str | None
    soul_text: str
    user_text: str
    heartbeat_text: str
    heartbeat_opt_in: bool
    heartbeat_frequency: str
    heartbeat_max_daily: int


class SharedDataHeartbeatLogItem(BaseModel):
    """One heartbeat scheduler run for a consenting user.

    The three redacted text columns (``message_text``, ``reasoning``,
    ``tasks``) are envelope-encrypted at rest; the ORM read decrypts
    transparently and ``redact_pii`` runs on each before serialization.
    """

    id: int
    action_type: str
    channel: str
    message_text: str
    reasoning: str
    tasks: str
    created_at: str | None


class SharedDataHeartbeatLogListResponse(BaseModel):
    user_id: str
    consent_at: str | None
    items: list[SharedDataHeartbeatLogItem]
    total: int


class SharedDataMemoryDocumentResponse(BaseModel):
    """Working memory + accumulated compaction history for a consenting user.

    ``memory_text`` is the agent's working memory file; ``history_text``
    is the chronological log of what compaction extracted from older
    sessions. Per-event metadata (when, sizes, costs, what got updated)
    lives in ``compaction_events`` and is exposed via the sibling
    ``/compaction-events`` endpoint; this response is the working
    memory itself.
    """

    user_id: str
    consent_at: str | None
    memory_text: str
    history_text: str
    updated_at: str | None


class SharedDataCompactionSnapshot(BaseModel):
    """One before/after memory-file snapshot from a compaction event.

    OSS stores eight envelope-encrypted text columns on
    ``compaction_events`` (memory/history/user/soul x before/after). The
    ORM decrypts to plaintext on read. When the underlying file
    exceeded ``settings.compaction_event_snapshot_max_bytes_per_file``
    the column instead carries a JSON truncation record (``head``,
    ``tail``, ``size_bytes``, ``sha256``); we surface that here as
    ``truncated=True`` so the admin UI can render "truncated, N KB" with
    the head and tail visible inline rather than dumping the JSON
    verbatim into a body cell.

    ``None`` plaintext (``text`` is None and ``truncated`` is False)
    means the field was unchanged by this event (skip-if-unchanged
    optimization), the row is still ``'pending'``, or the row predates
    the feature.
    """

    text: str | None = None
    truncated: bool = False
    size_bytes: int | None = None
    head: str | None = None
    tail: str | None = None
    sha256: str | None = None


class SharedDataCompactionEventItem(BaseModel):
    """One persisted compaction event for a consenting user.

    Mirrors ``backend.app.models.CompactionEvent``. Counts, timings,
    and outcome flags are metadata, so no redaction is applied.
    ``status`` is one of ``'pending'`` (sync trim watermark advanced,
    async LLM call still running or crashed) or ``'completed'`` (LLM
    call finished and snapshots populated). The eight ``*_before`` /
    ``*_after`` snapshots carry plaintext (decrypted on read) of the
    four memory files this event touched, or a structured
    :class:`SharedDataCompactionSnapshot` truncation record when the
    plaintext exceeded the per-file cap.
    """

    id: int
    triggered_at: str | None
    duration_ms: int
    trimmed_count: int
    trimmed_chars: int
    input_tokens: int
    output_tokens: int
    min_message_seq: int | None
    max_message_seq: int | None
    status: str
    memory_updated: bool
    user_profile_updated: bool
    soul_updated: bool
    summary_len: int
    memory_text_before: SharedDataCompactionSnapshot
    memory_text_after: SharedDataCompactionSnapshot
    history_text_before: SharedDataCompactionSnapshot
    history_text_after: SharedDataCompactionSnapshot
    user_text_before: SharedDataCompactionSnapshot
    user_text_after: SharedDataCompactionSnapshot
    soul_text_before: SharedDataCompactionSnapshot
    soul_text_after: SharedDataCompactionSnapshot
    # Capture of the actual compaction LLM call (OSS migration 031).
    # ``prompt`` is the trimmed conversation block fed to the LLM,
    # ``raw_response`` is the unparsed model output before
    # ``_parse_compaction_response`` runs (catches malformed JSON), and
    # ``parsed_response`` is a JSON-serialized ``CompactionResult`` so
    # the four parsed-field strings are inspectable. All three reuse
    # the snapshot envelope (plaintext-or-truncation-record) and decode
    # path so they share PII redaction with the memory-file snapshots
    # above. Pending events have all three empty until the async LLM
    # call lands and flips ``status`` to ``'completed'``.
    prompt: SharedDataCompactionSnapshot
    raw_response: SharedDataCompactionSnapshot
    parsed_response: SharedDataCompactionSnapshot


class SharedDataCompactionEventListResponse(BaseModel):
    user_id: str
    consent_at: str | None
    items: list[SharedDataCompactionEventItem]
    total: int


class SharedDataApprovalEventItem(BaseModel):
    """One persisted tool-approval lifecycle transition for a consenting user.

    Mirrors ``backend.app.models.ApprovalEvent``. ``event_type`` is one of
    ``requested`` / ``decided`` / ``timed_out`` / ``recovered``;
    ``decision`` is populated only on ``decided`` rows. ``description``
    is the human-readable string that was shown to the user in the
    approval prompt and can echo user-pasted content (filenames, URLs),
    so it's PII-redacted before serialization. ``channel`` and
    ``chat_id`` are infrastructure metadata; they are not redacted
    because they identify routing, not content.
    """

    id: int
    event_type: str
    tool_name: str
    description: str
    channel: str
    chat_id: str
    decision: str | None
    created_at: str | None


class SharedDataApprovalEventListResponse(BaseModel):
    user_id: str
    consent_at: str | None
    items: list[SharedDataApprovalEventItem]
    total: int


# ---------------------------------------------------------------------------
# Admin: per-user export bundle
# ---------------------------------------------------------------------------
#
# Single composite response that bundles every consent-gated surface for
# one user across a date window. Designed for CLI / offline analysis:
# ``curl /admin/shared-data/users/{id}/export?days=7 | jq ...`` should
# answer most "is this user's experience working?" questions in one
# request, without the admin having to walk the per-surface endpoints
# by hand.
#
# Heavy fields (turn-grouped transcripts) stay opt-in via
# ``include_turns=true`` because they're expensive both to compute and
# to ship.


class SharedDataExportTopTool(BaseModel):
    """One tool name with its call frequency and error count."""

    name: str
    call_count: int
    error_count: int


class SharedDataExportSummary(BaseModel):
    """Aggregate counts for one user over the requested window.

    Mirrors the per-user activity rollup an admin would otherwise
    derive by walking the conversations / heartbeat-logs / compaction
    endpoints. Counts only; bodies and memory text live in the other
    fields of the export response.
    """

    session_count: int
    message_count: int
    inbound_count: int
    outbound_count: int
    heartbeats_total: int
    heartbeats_by_action: dict[str, int]
    compactions_count: int
    llm_calls_total: int
    llm_calls_by_purpose: dict[str, int]
    llm_cost_usd: str
    llm_input_tokens: int
    llm_output_tokens: int
    llm_cache_read_tokens: int
    tool_calls_total: int
    tool_calls_error_count: int
    tool_calls_top: list[SharedDataExportTopTool]
    # Reports filed by this user with ``created_at`` inside the window.
    # Cumulative reports are reachable via the dedicated
    # ``/admin/reported-conversations`` endpoints; the windowed count
    # here lines up with the other time-bucketed counts in this rollup
    # (heartbeats_total, message_count, llm_calls_total).
    reports_total: int


class SharedDataExportResponse(BaseModel):
    """Composite per-user export.

    Sections:
    * ``user``: identity + profile config (timezone, channel, consent
      timestamp).
    * ``window``: the date range applied to time-windowed sub-resources.
    * ``summary``: the aggregate-counts rollup.
    * ``conversations``: per-session metadata for sessions active in
      the window (no bodies; bodies live in ``turns`` if requested).
    * ``heartbeat_logs``: per-event rows including PII-redacted
      ``message_text`` / ``reasoning`` / ``tasks``.
    * ``compaction_events``: per-event metadata (no content; the
      content is in ``memory.history_text``).
    * ``profile``: soul / user / heartbeat directives text, PII-redacted.
    * ``memory``: working memory + compacted history, PII-redacted.
    * ``turns``: turn-grouped transcripts (only when ``include_turns=true``).
    """

    user_id: str
    user: dict[str, str | None | bool]
    window: dict[str, str | int]
    summary: SharedDataExportSummary
    conversations: list[SharedDataConversationItem]
    heartbeat_logs: list[SharedDataHeartbeatLogItem]
    compaction_events: list[SharedDataCompactionEventItem]
    profile: SharedDataProfileResponse
    memory: SharedDataMemoryDocumentResponse
    # Heavy: only populated when ``include_turns=true``.
    turns: list[SharedDataConversationTurnsResponse] | None = None


# ---------------------------------------------------------------------------
# Admin: reported conversations (issue #325 item 5)
# ---------------------------------------------------------------------------


class ReportedConversationItem(BaseModel):
    """One ``ReportedConversation`` row in the admin queue.

    The ``reason`` field is the user-supplied free-text passed through
    PII redaction at serialization time; the raw stored value is never
    surfaced. ``status`` is derived: ``"open"`` until ``dismissed_at``
    is set, then ``"dismissed"``.
    """

    id: int
    user_id: str
    user_email: str
    session_id: str
    channel: str
    anchor_seq: int | None
    reason: str
    status: str
    created_at: str
    dismissed_at: str | None
    reviewed_admin_email: str | None


class ReportedConversationListResponse(BaseModel):
    total: int
    open_count: int
    items: list[ReportedConversationItem]


class ReportedConversationMessage(BaseModel):
    """One message in a reported conversation, PII-redacted.

    Mirrors ``SharedDataMessageItem`` shape so the frontend can reuse
    the same renderer. ``is_anchor`` flags the message that the
    ``/report`` command anchored against, so the UI can highlight the
    surrounding window.
    """

    seq: int
    direction: str
    body: str
    timestamp: str | None
    is_anchor: bool


class ReportedConversationMessageListResponse(BaseModel):
    report_id: int
    session_id: str
    user_id: str
    anchor_seq: int | None
    items: list[ReportedConversationMessage]


class DismissReportedConversationResponse(BaseModel):
    id: int
    dismissed_at: str
    reviewed_admin_user_id: str


# ---------------------------------------------------------------------------
# Admin API keys
# ---------------------------------------------------------------------------


class AdminApiKeyCreate(BaseModel):
    """Mint request body. ``label`` is free-form, capped server-side."""

    label: str = ""


class AdminApiKeyItem(BaseModel):
    """One row in the admin's key list. Cleartext token never appears."""

    id: int
    label: str
    key_prefix: str
    created_at: str
    last_used_at: str | None
    revoked_at: str | None


class AdminApiKeyListResponse(BaseModel):
    items: list[AdminApiKeyItem]


class AdminApiKeyMintResponse(BaseModel):
    """Mint response. ``token`` is the only place the cleartext is shown.

    The caller must save the token immediately; subsequent reads of
    the row only have the prefix. ``key_prefix`` is duplicated here
    so the frontend can echo it back to the admin alongside the
    cleartext for confirmation ("you minted ``ck_a1b2c3d4...``").
    """

    id: int
    token: str
    key_prefix: str
    label: str
    created_at: str


class HygieneCompactMemoryResponse(BaseModel):
    """Outcome of an admin-triggered hygiene-only memory re-audit.

    ``memory_updated`` indicates whether at least one exclusion-list
    violation was removed from MEMORY.md. ``memory_text`` carries the
    new full MEMORY.md content (empty string if nothing changed) so
    the admin can preview the diff without re-reading the user's
    memory file.
    """

    memory_updated: bool
    memory_text: str


# ---------------------------------------------------------------------------
# Model-swap evaluator (admin)
# ---------------------------------------------------------------------------


class AdminLLMEvalRunCreate(BaseModel):
    """Request to replay a user's recent turns against a candidate model.

    The baseline is not accepted from the client: it is resolved server-side
    from the user's effective configuration (their subscription override, or
    the global default), so a report can never compare against a model the
    user was not actually on.
    """

    candidate_provider: str = Field(min_length=1, max_length=64)
    candidate_model: str = Field(min_length=1, max_length=128)
    sample_count: int = Field(default=100, ge=1)
    judge_enabled: bool = True


class AdminLLMEvalModelTotals(BaseModel):
    """Cost, cache, and latency totals for one model across a run."""

    provider: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_ratio: float = 0.0
    total_cost_usd: str = "0.000000"
    # False when genai-prices has no entry for this (provider, model). The
    # cost above is then zero and must not be read as "free".
    pricing_available: bool = True
    latency_p50_ms: float = 0.0
    latency_p95_ms: float = 0.0


class AdminLLMEvalSummary(BaseModel):
    """The frozen aggregate stored on the run when it completed."""

    turns_total: int = 0
    turns_completed: int = 0
    turns_failed: int = 0
    agreement_counts: dict[str, int] = Field(default_factory=dict)
    safety_counts: dict[str, int] = Field(default_factory=dict)
    # Subset of ``safety_counts`` that actually disqualifies a switch. A
    # provider error is recorded above but is a failure to measure, not
    # something the candidate did, so it is excluded here.
    blocking_findings: int = 0
    judge_counts: dict[str, int] = Field(default_factory=dict)
    identical_rate: float = 0.0
    divergence_rate: float = 0.0
    silent_noop_rate: float = 0.0
    baseline: AdminLLMEvalModelTotals = Field(default_factory=AdminLLMEvalModelTotals)
    candidate: AdminLLMEvalModelTotals = Field(default_factory=AdminLLMEvalModelTotals)
    recommendation: str = ""
    reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class AdminLLMEvalRunItem(BaseModel):
    """One run, without its per-turn evidence."""

    id: int
    user_id: str
    baseline_provider: str
    baseline_model: str
    candidate_provider: str
    candidate_model: str
    judge_model: str
    requested_samples: int
    status: str
    progress_completed: int
    progress_total: int
    recommendation: str
    error: str
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    summary: AdminLLMEvalSummary | None = None


class AdminLLMEvalRunListResponse(BaseModel):
    runs: list[AdminLLMEvalRunItem]


class AdminLLMEvalSafetyIssue(BaseModel):
    finding: str
    tool_name: str = ""
    detail: str = ""


class AdminLLMEvalToolCall(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class AdminLLMEvalDecision(BaseModel):
    """One model's decision for one replayed turn."""

    text: str = ""
    tool_calls: list[AdminLLMEvalToolCall] = Field(default_factory=list)
    stop_reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    latency_ms: float = 0.0
    error: str = ""


class AdminLLMEvalTurn(BaseModel):
    """One replayed turn: the user's message and both models' decisions.

    ``historic_reply`` and ``historic_tool_names`` are what the agent actually
    did for this turn when it happened. They are shown alongside, not scored:
    that turn ran against an older system prompt and an older tool set, so it
    is context for a human reading the diff rather than a third contestant.
    """

    message_seq: int
    message_timestamp: str
    user_message: str
    historic_reply: str = ""
    historic_tool_names: list[str] = Field(default_factory=list)
    baseline: AdminLLMEvalDecision
    candidate: AdminLLMEvalDecision
    agreement: str
    safety_issues: list[AdminLLMEvalSafetyIssue] = Field(default_factory=list)
    judge_verdict: str = "not_judged"
    judge_rationale: str = ""


class AdminLLMEvalReportResponse(BaseModel):
    """A run plus a page of its per-turn evidence, worst turns first."""

    run: AdminLLMEvalRunItem
    turns: list[AdminLLMEvalTurn]
    # Total turns stored for the run, so a caller can tell whether the page
    # it received is the whole story.
    total_turns: int = 0
