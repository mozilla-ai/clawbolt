"""Consent-gated access to real user content, PII-redacted at serialization."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


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
