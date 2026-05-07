import hashlib
import hmac
import logging
from typing import Any

from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


def _derive_webhook_secret(bot_token: str) -> str:
    """Derive a deterministic webhook secret from the bot token via HMAC-SHA256."""
    return hmac.new(
        key=b"backshop-telegram-webhook-secret",
        msg=bot_token.encode(),
        digestmod=hashlib.sha256,
    ).hexdigest()


def get_effective_webhook_secret(s: "Settings") -> str:
    """Return the explicit secret if set, otherwise derive one from the bot token."""
    if s.telegram_webhook_secret:
        return s.telegram_webhook_secret
    if s.telegram_bot_token:
        return _derive_webhook_secret(s.telegram_bot_token)
    return ""


class Settings(BaseSettings):
    log_level: str = "INFO"
    data_dir: str = "data/users"
    database_url: str = "postgresql://clawbolt:clawbolt@localhost:5432/clawbolt"
    cors_origins: str = "http://localhost:3000,http://localhost:8000"
    jwt_secret: str = "change-me-in-production"
    jwt_expiry_minutes: int = Field(default=15, ge=1)
    premium_plugin: str | None = None
    # Backend for runtime-configurable settings: "db" (default) stores in
    # the app_settings table; "file" keeps the legacy data/config.json
    # behavior for file-based deployments.
    settings_store: str = "db"

    # Messaging
    messaging_provider: str = "telegram"
    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""
    telegram_allowed_chat_id: str = ""  # Single numeric chat ID, or "*" for all; empty = deny all

    # LLM
    llm_provider: str = ""
    llm_model: str = ""
    llm_api_base: str | None = None
    vision_model: str = ""  # empty = fall back to llm_model
    vision_provider: str = ""  # empty = fall back to llm_provider
    reasoning_effort: str = "auto"  # none, minimal, low, medium, high, xhigh, auto
    # 2048 is sized to fit a typical multi-tool turn (one ~200-token reply
    # plus a tool call whose JSON args can run 500-1500 tokens for nested
    # entity payloads in the QuickBooks / CompanyCam tools). The previous
    # 1024 default truncated mid-tool-call on real workloads, leaving the
    # validator to catch the malformed args; auto-recovery in
    # ``core.py:_call_llm_with_retry`` doubles ``max_tokens`` on the next
    # round, but a higher floor avoids the wasted round entirely.
    llm_max_tokens_agent: int = Field(default=2048, ge=1)
    llm_max_tokens_heartbeat: int = Field(default=12000, ge=1)
    llm_max_tokens_vision: int = Field(default=1000, ge=1)

    # Storage: per-user Google Drive via OAuth. The deployment supplies the
    # OAuth client credentials; each user grants ``drive.file`` scope through
    # ``manage_integration(action='connect', target='google_drive')``. Files
    # land in the user's own Drive, not a shared admin Drive.
    google_drive_client_id: str = ""
    google_drive_client_secret: str = ""

    # Agent loop
    approval_timeout_seconds: int = Field(default=120, ge=1)
    agent_processing_timeout_seconds: float = Field(default=300.0, gt=0)
    message_batch_window_ms: int = Field(default=1500, ge=100)
    # Inbound messages are persisted before MessageBatcher schedules an
    # in-memory flush task. If the worker dies during that window the
    # message lives in the DB but never reaches the agent. On startup we
    # sweep for inbound rows from the last N minutes that have no
    # outbound after them and re-dispatch each. Older orphans are
    # unlikely to still be relevant; tune up if you have evidence
    # otherwise. 0 disables the sweep entirely.
    inbound_recovery_lookback_minutes: int = Field(default=30, ge=0)
    max_tool_rounds: int = Field(default=10, ge=1)
    max_input_tokens: int = Field(default=600_000, ge=1)
    context_trim_target_tokens: int = Field(default=400_000, ge=1)
    # Cap user turns kept verbatim in LLM context. Long single-conversation
    # histories reinforce their own dominant tone, so new requests inherit
    # prior conversational patterns rather than optimal procedural ones.
    # Trimming oldest turns past this cap (independent of token budget)
    # rolls them through compaction into MEMORY.md / USER.md / SOUL.md.
    # Set ge=2 so at least one prior turn is always retained alongside the
    # current one. Tune up if compaction proves too aggressive.
    context_trim_target_turns: int = Field(default=80, ge=2)
    # Trim trigger threshold: trim only fires when user-turn count exceeds
    # this. Trim then drops down to ``context_trim_target_turns``, leaving
    # ``trigger - target`` turns of headroom before the next trim fires.
    # A single threshold (target == trigger) re-fires compaction on every
    # message after the first overflow because the resting state sits
    # exactly at the ceiling. When ``None``, defaults to
    # ``context_trim_target_turns + 16`` inside ``trim_messages``.
    context_trim_trigger_turns: int | None = Field(default=None)
    # Per-file truncation cap for memory-text snapshots persisted on
    # ``compaction_events`` rows. A user with a 500KB MEMORY.md would
    # otherwise produce ~4MB rows once before/after for all four files
    # (memory, history, user, soul) is included. When a file exceeds the
    # cap, the snapshot column stores a structured truncation record
    # (head, tail, size, sha256) rather than the full text. Bounds the
    # worst-case row size while keeping admin diff visibility intact.
    compaction_event_snapshot_max_bytes_per_file: int = Field(default=100_000, ge=1024)
    llm_max_retries: int = Field(default=3, ge=1)
    # Use Anthropic's 1-hour extended-TTL cache instead of the default
    # 5-minute ephemeral cache. Inactive users with conversation gaps
    # >5 min currently always miss the prompt cache on their first
    # turn after returning. The 1h TTL covers their typical re-engage
    # pattern at a 1.5x cache_create premium (vs 1.25x for 5min);
    # the read cost is unchanged. Net cost goes down for any user
    # whose median inter-message gap is more than ~5 min.
    # Set to ``False`` to opt back into the default 5-minute TTL,
    # e.g. if a non-Anthropic provider rejects the ttl field.
    llm_cache_extended_ttl: bool = True

    # Conversation & memory
    conversation_history_limit: int = Field(default=500, ge=1)
    memory_recall_limit: int = Field(default=20, ge=1)
    compaction_enabled: bool = True
    compaction_model: str = ""  # empty = fall back to llm_model
    compaction_provider: str = ""  # empty = fall back to llm_provider
    compaction_max_tokens: int = Field(default=16_000, ge=1)

    # Rate limiting
    webhook_rate_limit_max_requests: int = Field(default=30, ge=1)
    webhook_rate_limit_window_seconds: int = Field(default=60, ge=1)
    rate_limit_trust_proxy: bool = False

    # Unknown-sender reply (sent when a non-allowlisted number messages us;
    # rate-limited per sender so we can't be used as a spam relay).
    unknown_sender_signup_url: str = ""
    unknown_sender_reply_cooldown_seconds: int = Field(default=86_400, ge=0)

    # Media
    max_media_size_bytes: int = Field(default=20_971_520, ge=1)  # 20 MB
    # Hard wall-time ceiling for any single media download. Guards against
    # slow-drip carriers that keep the connection alive but never finish.
    media_download_max_seconds: float = Field(default=60.0, gt=0)

    # QuickBooks Online
    quickbooks_client_id: str = ""
    quickbooks_client_secret: str = ""
    quickbooks_environment: str = "sandbox"  # "sandbox" or "production"

    # Linq (iMessage/RCS/SMS)
    linq_api_token: str = ""
    linq_from_number: str = ""  # E.164 format
    linq_webhook_signing_secret: str = ""
    linq_allowed_numbers: str = ""  # E.164 phone number, "*", or empty
    linq_preferred_service: str = "iMessage"  # "iMessage", "SMS", or "RCS"

    # BlueBubbles (self-hosted iMessage bridge)
    bluebubbles_server_url: str = ""  # e.g. "https://my-mac.ngrok.io"
    bluebubbles_password: str = ""  # server password (query param auth)
    bluebubbles_allowed_numbers: str = ""  # E.164 phone, "*", or empty (deny all)
    bluebubbles_send_method: str = "apple-script"  # "apple-script" or "private-api"
    bluebubbles_imessage_address: str = ""  # iCloud email or phone to display in the UI
    # On startup, query the BlueBubbles server for messages received in the
    # last N minutes and replay any whose webhook never reached us (Clawbolt
    # was down or unreachable). Dedup is structural: the idempotency store
    # rejects messages we already processed via the live webhook path.
    # 0 disables the sweep entirely. Tune up for tolerance of longer
    # outages, down for stricter "no replies to stale messages" behavior.
    bluebubbles_backfill_lookback_minutes: int = Field(default=30, ge=0)

    # Google Calendar
    google_calendar_client_id: str = ""
    google_calendar_client_secret: str = ""

    # CompanyCam OAuth 2.0
    companycam_client_id: str = ""
    companycam_client_secret: str = ""
    # Web app base URL for receipt deep links. Override if CompanyCam ever
    # ships EU / sandbox hosts (the US prod URL is stable today).
    companycam_web_base: str = "https://app.companycam.com"

    # Supplier pricing (SerpApi Home Depot engine)
    serpapi_api_key: str = ""  # https://serpapi.com — free tier: 250 searches/month

    # OAuth
    app_base_url: str = "http://localhost:8000"  # Public URL for OAuth callbacks

    # Encryption (used for OAuth tokens at rest; generate with: python -c "import secrets; print(secrets.token_urlsafe(32))")
    encryption_key: SecretStr = SecretStr("")

    # HTTP timeouts
    http_timeout_seconds: float = Field(default=30.0, gt=0)
    cloudflared_metrics_timeout_seconds: float = Field(default=5.0, gt=0)
    telegram_webhook_timeout_seconds: float = Field(default=10.0, gt=0)

    # Heartbeat
    heartbeat_enabled: bool = True
    heartbeat_default_frequency: str = "30m"
    heartbeat_interval_minutes: int = Field(default=30, ge=1)
    heartbeat_max_daily_messages: int = Field(default=5, ge=1)
    heartbeat_model: str = ""  # empty = fall back to llm_model
    heartbeat_provider: str = ""  # empty = fall back to llm_provider
    heartbeat_concurrency: int = Field(default=5, ge=1)
    heartbeat_recent_messages_count: int = Field(default=5, ge=1)
    # Skip the heartbeat LLM call for a user who messaged recently. The
    # scheduler ticks every ``heartbeat_interval_minutes`` regardless of
    # user activity; without this gate, an active conversation produces
    # a tick → LLM call → "skip" decision every interval, burning tokens
    # for no user value. The default 5-minute window is short enough not
    # to delay genuinely overdue nudges and long enough to absorb a
    # multi-turn back-and-forth. Set to 0 to disable the throttle.
    heartbeat_user_quiet_period_minutes: int = Field(default=5, ge=0)
    # Delay the first scheduler tick after process start. Without this,
    # a deploy mid-conversation produces a tick on the new container
    # within ~2 seconds of boot, before any in-flight work on the old
    # container has had a chance to settle, and before any queued
    # inbound messages have drained from the bus into normal processing.
    # The Phase 1 LLM then sees the user's pending request in recent
    # context and decides to act, racing the agent path that would have
    # handled it normally. 60 seconds is short enough not to delay
    # genuine proactive nudges in long-running deployments and long
    # enough to absorb the post-restart settle window. Set to 0 to
    # disable the warmup (the previous behavior).
    heartbeat_startup_warmup_seconds: int = Field(default=60, ge=0)

    # Observability
    log_request_timing: bool = False  # Set True (or LOG_REQUEST_TIMING=1) to log per-request timing

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()

TELEGRAM_API_BASE = "https://api.telegram.org"

# ---------------------------------------------------------------------------
# Persistable settings -- runtime-configurable values stored by SettingsStore.
# ---------------------------------------------------------------------------

# Allowlist of keys the admin UI is allowed to mutate at runtime. The
# active SettingsStore (DB or file, see backend.app.config_store) reads
# and writes only these keys; everything else is process-startup-only.
PERSISTABLE_SETTINGS: frozenset[str] = frozenset(
    {
        "telegram_bot_token",
        "telegram_allowed_chat_id",
        "telegram_webhook_secret",
        "linq_api_token",
        "linq_from_number",
        "linq_webhook_signing_secret",
        "linq_allowed_numbers",
        "linq_preferred_service",
        "bluebubbles_server_url",
        "bluebubbles_password",
        "bluebubbles_allowed_numbers",
        "bluebubbles_send_method",
        "bluebubbles_imessage_address",
        "llm_provider",
        "llm_model",
        "llm_api_base",
        "llm_max_tokens_agent",
        "llm_max_tokens_heartbeat",
        "llm_max_tokens_vision",
        "vision_model",
        "vision_provider",
        "heartbeat_model",
        "heartbeat_provider",
        "compaction_model",
        "compaction_provider",
        "compaction_max_tokens",
        "reasoning_effort",
    }
)


def update_settings(updates: dict[str, Any]) -> None:
    """Validate and apply runtime updates to the settings singleton.

    Only keys listed in ``PERSISTABLE_SETTINGS`` are accepted.  Each value is
    validated against the Pydantic field definition before being applied, so
    type mismatches raise ``ValueError``.

    Coerced values from validation are what get applied, so a non-string
    field persisted as a string in the store (e.g. an int read back from
    the TEXT ``app_settings.value`` column) lands on the singleton as
    the correct type. Without this, code reading the field would get a
    raw string and crash on type-specific operations.

    Validation runs for all keys before any are applied, so a failure on one
    key never leaves the singleton in a partially-updated state.
    """
    coerced: dict[str, Any] = {}
    for key, value in updates.items():
        if key not in PERSISTABLE_SETTINGS:
            raise ValueError(
                f"{key!r} is not a persistable setting (allowed: {sorted(PERSISTABLE_SETTINGS)})"
            )
        try:
            validated = Settings.model_validate({key: value})
        except ValidationError as exc:
            raise ValueError(str(exc)) from exc
        coerced[key] = getattr(validated, key)

    for key, value in coerced.items():
        setattr(settings, key, value)


def resolve_imessage_backend(s: "Settings | None" = None) -> str | None:
    """Return the configured iMessage backend: "linq", "bluebubbles", or None.

    Users of the product never see the backend name. This helper is the single
    source of truth for which backend powers the user-facing iMessage channel.
    """
    s = s or settings
    linq_set = bool(s.linq_api_token)
    bluebubbles_set = bool(s.bluebubbles_server_url and s.bluebubbles_password)
    if linq_set:
        return "linq"
    if bluebubbles_set:
        return "bluebubbles"
    return None


def validate_imessage_backend(s: "Settings | None" = None) -> None:
    """Reject startup if both iMessage backends are configured simultaneously.

    The UI surfaces a single iMessage channel; allowing both backends at once
    would make that card's behavior ambiguous. Operators must pick one.
    """
    s = s or settings
    linq_set = bool(s.linq_api_token)
    bluebubbles_set = bool(s.bluebubbles_server_url and s.bluebubbles_password)
    if linq_set and bluebubbles_set:
        raise RuntimeError(
            "Two iMessage backends are configured at once. "
            "Set only LINQ_API_TOKEN or only BLUEBUBBLES_SERVER_URL + "
            "BLUEBUBBLES_PASSWORD, not both."
        )


def log_config_warnings(s: Settings | None = None) -> list[str]:
    """Log warnings for unusual but valid config values. Returns the warnings."""
    s = s or settings
    warnings: list[str] = []

    if s.max_tool_rounds > 50:
        warnings.append(f"max_tool_rounds={s.max_tool_rounds} is unusually high (default: 10)")
    if s.message_batch_window_ms > 10_000:
        warnings.append(
            f"message_batch_window_ms={s.message_batch_window_ms} is unusually high (default: 1500)"
        )
    if s.llm_max_tokens_agent < 100:
        warnings.append(
            f"llm_max_tokens_agent={s.llm_max_tokens_agent} is very low"
            " and may produce truncated responses"
        )
    if s.context_trim_target_tokens >= s.max_input_tokens:
        warnings.append(
            f"context_trim_target_tokens ({s.context_trim_target_tokens})"
            f" >= max_input_tokens ({s.max_input_tokens});"
            " trimming will never trigger"
        )

    # Warn when an iMessage backend is configured but the address users are
    # supposed to text isn't set. The channel picker UI falls back to generic
    # copy in that case, leaving users with no idea where to send messages.
    backend = resolve_imessage_backend(s)
    if backend == "linq" and not s.linq_from_number:
        warnings.append(
            "LINQ_API_TOKEN is set but LINQ_FROM_NUMBER is empty;"
            " the iMessage channel picker will not show an address for users to text"
        )
    elif backend == "bluebubbles" and not s.bluebubbles_imessage_address:
        warnings.append(
            "BlueBubbles is configured but BLUEBUBBLES_IMESSAGE_ADDRESS is empty;"
            " the iMessage channel picker will not show an address for users to text"
        )

    enc_key = s.encryption_key.get_secret_value()
    if not enc_key:
        warnings.append(
            "encryption_key is not set; OAuth tokens will be stored unencrypted."
            " Set ENCRYPTION_KEY to a random value"
            ' (python -c "import secrets; print(secrets.token_urlsafe(32))")'
        )
    elif len(enc_key) < 16:
        warnings.append(
            f"encryption_key is only {len(enc_key)} characters;"
            " use at least 32 characters of random data for production"
        )

    for w in warnings:
        logger.warning("Config: %s", w)

    return warnings
