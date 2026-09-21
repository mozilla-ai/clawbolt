"""Admin console: users, quotas, waitlist, diagnostics, and API keys."""

from __future__ import annotations

from pydantic import BaseModel, Field


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


class DeleteResponse(BaseModel):
    deleted: bool
    id: int


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
