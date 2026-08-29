import hashlib
import hmac
import logging
import os
from typing import Any, Literal

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)

# Use any-llm's provider-independent exception types so retry and fallback logic
# can handle rate limits, context overflow, auth failures, and content filters.
# ``setdefault`` preserves an operator's explicit process-level override.
os.environ.setdefault("ANY_LLM_UNIFIED_EXCEPTIONS", "1")

# Default hysteresis buffer for the turn-count trim backstop: when
# ``context_trim_trigger_turns`` is unset, the trigger resolves to
# ``context_trim_target_turns + CONTEXT_TRIM_DEFAULT_TRIGGER_BUFFER_TURNS``
# (see ``trim_messages`` in backend/app/agent/trimming.py). Lives here, not
# in trimming.py, so ``log_config_warnings`` can compute the effective
# trigger without importing agent code.
CONTEXT_TRIM_DEFAULT_TRIGGER_BUFFER_TURNS: int = 16


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
    # Tenancy switch. Multi-user mode enables authenticated hosted surfaces;
    # single-user mode resolves requests to the deployment's sole user.
    auth_mode: Literal["single_user", "multi_user"] = "single_user"
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
    # Allows reasoning plus nested tool payloads without truncating a tool call.
    # ``core.py`` applies a separate recovery ceiling to runaway generations.
    llm_max_tokens_agent: int = Field(default=8192, ge=1)
    llm_max_tokens_heartbeat: int = Field(default=12000, ge=1)
    llm_max_tokens_vision: int = Field(default=12000, ge=1)

    # Storage: per-user Google Drive via OAuth. The deployment supplies the
    # OAuth client credentials; each user grants ``drive.file`` scope through
    # ``manage_integration(action='connect', target='google_drive')``. Files
    # land in the user's own Drive, not a shared admin Drive.
    google_drive_client_id: str = ""
    google_drive_client_secret: str = ""

    # Persistent staging for inbound media bytes; metadata is in Postgres.
    # One application instance must own this path. See issue #1336.
    media_staging_base_dir: str = "data/staged_media"

    # Agent loop
    approval_timeout_seconds: int = Field(default=120, ge=1)
    agent_processing_timeout_seconds: float = Field(default=300.0, gt=0)
    message_batch_window_ms: int = Field(default=1500, ge=100)
    # Redispatch recently persisted inbound messages that lack an outbound reply.
    # Zero disables startup recovery.
    inbound_recovery_lookback_minutes: int = Field(default=30, ge=0)
    # Retry recent pending compactions at startup. Zero disables recovery.
    compaction_retry_lookback_minutes: int = Field(default=10_080, ge=0)
    max_tool_rounds: int = Field(default=10, ge=1)
    max_input_tokens: int = Field(default=600_000, ge=1)
    # Primary trim governor. Trigger above the target to provide hysteresis.
    context_trim_target_tokens: int = Field(default=120_000, ge=1)
    context_trim_trigger_tokens: int = Field(default=150_000, ge=1)
    # Backstop for many token-light turns. Must remain reachable inside
    # ``conversation_history_limit``; ``log_config_warnings`` enforces this.
    context_trim_target_turns: int = Field(default=200, ge=2)
    # None resolves to target plus CONTEXT_TRIM_DEFAULT_TRIGGER_BUFFER_TURNS.
    context_trim_trigger_turns: int | None = Field(default=None)
    # Per-file cap for compaction-event snapshots. Oversize content is stored
    # as a head/tail/hash record rather than full text.
    compaction_event_snapshot_max_bytes_per_file: int = Field(default=100_000, ge=1024)
    llm_max_retries: int = Field(default=3, ge=1)
    # Use Anthropic's one-hour cache TTL instead of the five-minute default.
    llm_cache_extended_ttl: bool = True
    # "auto" stamps supported Anthropic cache breakpoints; "never" disables them.
    llm_prompt_cache: Literal["auto", "never"] = "auto"

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
    # Re-run the backfill on this cadence (in addition to the one-shot at
    # startup) so a webhook lost mid-flight is recovered without waiting
    # for a deploy. BlueBubbles' webhook delivery is fire-and-forget with
    # no retry, so a transient receiver hiccup is otherwise unrecoverable
    # until the next restart. 0 disables the recurring sweep; the boot-time
    # sweep still runs. Default 5 minutes is well under what a contractor
    # would notice and short enough that ``_BACKFILL_QUERY_LIMIT=200`` is
    # never close to saturating.
    bluebubbles_backfill_interval_seconds: int = Field(default=300, ge=0)
    # Re-check ``/api/v1/server/info`` on this cadence so the dashboard
    # reachability light reflects current state rather than a snapshot
    # taken at boot. Matters more on premium where many tenants share one
    # Mac in someone's basement: when that Mac sleeps every tenant goes
    # silent and we want the signal surfaced immediately. 0 disables the
    # periodic check; the boot-time check still runs.
    bluebubbles_health_check_interval_seconds: int = Field(default=120, ge=0)

    # Twilio (RCS via Messaging Service, with SMS/MMS fallback). Register
    # an RCS Agent in the Twilio console and attach it to a Messaging
    # Service; Twilio routes RCS-capable recipients over RCS and falls
    # back to SMS/MMS automatically for everyone else.
    twilio_account_sid: str = ""
    # Auth token: required for inbound webhook signature validation.
    # Twilio signs ``X-Twilio-Signature`` with HMAC-SHA1 keyed on this
    # token and offers no alternative signing mechanism. The codebase
    # loads it for nothing else; outbound REST uses the API key pair
    # below.
    twilio_auth_token: str = ""
    # Standard API key + secret. Required for every outbound REST call
    # (send messages, media downloads). Create via Twilio Console >
    # Account > API Keys & Tokens (Standard key).
    twilio_api_key_sid: str = ""  # "SKxxxxxxxx..."
    twilio_api_key_secret: str = ""
    # Outbound sender. Pin a specific phone number (E.164) OR a Messaging
    # Service SID. Messaging Service is the right choice for RCS (the
    # agent attaches to the service) and for US A2P 10DLC pools. When
    # both are set, the Messaging Service SID wins.
    twilio_phone_number: str = ""  # E.164 format, e.g. "+15551234567"
    twilio_messaging_service_sid: str = ""  # "MGxxxxxxxx..."
    twilio_allowed_numbers: str = ""  # E.164 phone, "*", or empty (deny all)
    # Validate inbound webhook signatures via ``X-Twilio-Signature``. Off
    # in development is fine; ON in production. The validator needs the
    # exact public URL of the webhook, so behind a proxy / tunnel the
    # ``app_base_url`` setting must reflect what Twilio actually sees.
    twilio_validate_signatures: bool = True

    # Google Calendar
    google_calendar_client_id: str = ""
    google_calendar_client_secret: str = ""

    # Gmail: per-user Gmail API access via OAuth. The deployment supplies the
    # OAuth client credentials; each user grants the ``gmail.readonly`` and
    # ``gmail.send`` scopes through
    # ``manage_integration(action='connect', target='gmail')``. Read access
    # lets the agent search and fetch the user's own messages; send access
    # lets it compose new messages or thread replies on the user's behalf.
    gmail_client_id: str = ""
    gmail_client_secret: str = ""

    # CompanyCam OAuth 2.0
    companycam_client_id: str = ""
    companycam_client_secret: str = ""
    # Web app base URL for receipt deep links. Override if CompanyCam ever
    # ships EU / sandbox hosts (the US prod URL is stable today).
    companycam_web_base: str = "https://app.companycam.com"

    # AppFolio Vendor Portal (magic-link Bearer JWT, no client_id/secret).
    # Override the API base only for staging or test environments; production
    # is the host the SPA calls (window.CONFIG.vendorUrl).
    appfolio_vendor_api_base: str = "https://vendor.appf.io"
    # Web app base for receipt deep links and the URL users paste from.
    appfolio_vendor_web_base: str = "https://vendor.appfolio.com"

    # ServiceTitan (OAuth 2.0 client-credentials per tenant + app-level App Key).
    # Each tenant enters their tenant ID, client ID, and client secret in the
    # Clawbolt web app (not over chat, issue #1337); the operator wires the
    # app-level App Key here.
    # ``servicetitan_use_fake`` swaps the real API for the in-process fake
    # backend (see ``backend/app/integrations/servicetitan/_fake.py``); used
    # by tests and by the local dev loop until a real sandbox tenant is wired.
    servicetitan_app_key: str = ""
    servicetitan_api_base_url: str = "https://api.servicetitan.io"
    # ServiceTitan splits auth and resource traffic across two hosts.
    # Production: auth.servicetitan.io for tokens, api.servicetitan.io for
    # resources. Integration sandbox: auth-integration.servicetitan.io plus
    # api-integration.servicetitan.io. Operators flipping to the
    # integration environment must override both this and api_base_url.
    servicetitan_auth_base_url: str = "https://auth.servicetitan.io"
    servicetitan_use_fake: bool = True

    # Web search. One general-purpose search tool covering material prices,
    # product specs, and code requirements. Without a key the tool does not
    # load and the agent simply has no search capability.
    web_search_api_key: str = ""
    # Selects the backend from the provider registry in the web_search factory.
    # Brave is the only one implemented today; the seam exists so a second one
    # is a new module plus a registry entry, not a rewrite of the tool.
    web_search_provider: str = "brave"
    # Short by design: this runs inside a live message loop where the user is
    # waiting, so a slow provider must fail fast rather than hang the reply.
    web_search_timeout_seconds: float = Field(default=10.0, gt=0)
    # Absorbs a model's retry-and-reword loop within one conversation. Long
    # enough to save the duplicate call, short enough that a price checked
    # twice in a day is fetched twice.
    web_search_cache_ttl_seconds: int = Field(default=900, ge=0)
    web_search_max_results: int = Field(default=5, ge=1, le=20)

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
    # Skip heartbeat evaluation during an active conversation. Zero disables.
    heartbeat_user_quiet_period_minutes: int = Field(default=5, ge=0)
    # Let queued inbound work settle before the first post-start heartbeat tick.
    heartbeat_startup_warmup_seconds: int = Field(default=60, ge=0)

    # Observability
    log_request_timing: bool = False  # Set True (or LOG_REQUEST_TIMING=1) to log per-request timing
    # Log rendering: "text" or "json". JSON adds the request correlation ID
    # as a top-level key for log aggregators.
    log_format: str = "text"

    # Hide attachments when an upstream proxy cannot accept typical photo sizes.
    chat_web_attachments_enabled: bool = True

    # -----------------------------------------------------------------
    # Multi-user deployment
    #
    # Everything below is inert unless AUTH_MODE=multi_user. A
    # single-user self-host can ignore this whole block.
    # -----------------------------------------------------------------

    # Google OAuth. The sign-in flow derives its redirect URI from
    # APP_BASE_URL; GOOGLE_REDIRECT_URI is kept only for deployments that
    # pinned a non-default value before the switch.
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/api/auth/oauth/google/callback"

    # Session tokens. JWT_SECRET is shared with the OSS field above.
    jwt_access_token_expire_minutes: int = Field(default=15, ge=1)
    jwt_refresh_token_expire_days: int = Field(default=30, ge=1)
    jwt_algorithm: str = "HS256"

    # Cost protection: global daily message cap for free-tier users (0 = disabled)
    free_tier_daily_global_cap: int = Field(default=0, ge=0)

    # Comma-separated user_ids for legacy env-var admin access. No longer
    # consulted at request time; admin is granted exclusively by
    # ``Subscription.role``. Retained so
    # ``python -m backend.app.cli promote-env-admins`` can migrate them.
    admin_user_ids_raw: str = ""

    # Email address auto-promoted to admin role on first login. Normalized
    # to lowercase + stripped so comparison with incoming OAuth emails is
    # case-insensitive.
    admin_email: str = ""

    # Registration mode: "open" (anyone) or "restricted" (allowed_emails table only)
    registration_mode: str = "restricted"

    # Auth rate limiting
    auth_rate_limit_max_requests: int = Field(default=10, ge=1)
    auth_rate_limit_window_seconds: int = Field(default=60, ge=1)

    # OAuth state token expiry (minutes)
    oauth_state_expiry_minutes: int = Field(default=5, ge=1)

    # Inactive account cleanup thresholds (months, free tier only)
    inactive_warn_months: int = Field(default=11, ge=1)
    inactive_delete_months: int = Field(default=12, ge=1)

    # SMTP for transactional email (waitlist approvals, operator alerts).
    # When smtp_host is empty, the email sender is a no-op so dev/local
    # works without credentials and CI needs no secrets.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = ""

    # One SMTP operation timeout; the complete send is capped at twice this value.
    smtp_timeout_seconds: int = Field(default=10, ge=1)

    # Grouped and throttled ERROR-log email alerts. Requires SMTP and a recipient.
    alerts_enabled: bool = True
    alert_email: str = ""
    alert_flush_interval_seconds: int = Field(default=60, ge=1)
    alert_dedupe_minutes: int = Field(default=30, ge=1)
    alert_max_emails_per_hour: int = Field(default=20, ge=1)

    # Dependency probes alert after the failure threshold and on recovery.
    health_monitor_enabled: bool = True
    health_check_interval_seconds: int = Field(default=300, ge=1)
    health_failure_threshold: int = Field(default=2, ge=1)

    # The LLM probe makes one single-token completion per tick.
    health_probe_llm: bool = True

    # Cap on users checked per tick by the integration probe. Each user
    # costs one auth_check per specialist factory (mostly cheap DB reads),
    # so an unbounded sweep would grow with the tenant count. Truncation is
    # logged, never silent.
    health_probe_max_users: int = Field(default=50, ge=1)

    # Per-probe ceiling. Probes call out to a residential Mac and an LLM
    # provider, neither of which is guaranteed to answer or to fail fast.
    # Without a ceiling one wedged socket stalls
    # the whole run, which is what made the admin tab's "Run probes now"
    # sit on "Running" indefinitely. A probe past this budget is reported
    # DOWN with a timeout detail, which is the honest reading: a dependency
    # that cannot answer in 45s is not healthy.
    health_probe_timeout_seconds: int = Field(default=45, ge=1)

    # AWS KMS for envelope encryption. When kms_key_arn is set,
    # ``auth.loader.get_kek_provider()`` returns a KMSEnvelopeKEKProvider;
    # when unset it falls back to LocalKEKProvider. This lets the code ship
    # ahead of platform engineering provisioning the key and IAM
    # credentials: flipping the env vars activates KMS on the next restart
    # with no code change.
    kms_key_arn: str = ""
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""

    @field_validator("admin_email", "alert_email", mode="before")
    @classmethod
    def _normalize_operator_email(cls, value: object) -> object:
        if isinstance(value, str):
            return value.lower().strip()
        return value

    @field_validator("kms_key_arn", mode="before")
    @classmethod
    def _strip_kms_key_arn(cls, value: object) -> object:
        """Reject whitespace-only ARNs at startup instead of letting the
        provider constructor crash on the first credential read.

        Whitespace alone is treated as the dormant signal (empty), so an
        operator who set ``KMS_KEY_ARN=" "`` by accident gets the local
        fallback rather than a runtime crash.
        """
        if isinstance(value, str):
            return value.strip()
        return value

    @model_validator(mode="after")
    def _validate_smtp_pair(self) -> "Settings":
        """Reject partial SMTP config so a typo'd env var fails loudly.

        Both ``SMTP_HOST`` and ``SMTP_FROM_EMAIL`` must be set for sends to
        work. Setting only one is almost certainly a misconfiguration;
        without this check the sender silently no-ops and operators
        discover the problem only when users complain about missing email.
        """
        host_set = bool(self.smtp_host.strip())
        from_set = bool(self.smtp_from_email.strip())
        if host_set != from_set:
            missing = "SMTP_FROM_EMAIL" if host_set else "SMTP_HOST"
            raise ValueError(
                f"SMTP config is partial: set both SMTP_HOST and SMTP_FROM_EMAIL, "
                f"or neither. Missing: {missing}."
            )
        return self

    @property
    def admin_user_ids(self) -> set[str]:
        """Parse comma-separated legacy admin user IDs into a set."""
        if not self.admin_user_ids_raw:
            return set()
        return {uid.strip() for uid in self.admin_user_ids_raw.split(",") if uid.strip()}

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
        "twilio_account_sid",
        "twilio_auth_token",
        "twilio_api_key_sid",
        "twilio_api_key_secret",
        "twilio_phone_number",
        "twilio_messaging_service_sid",
        "twilio_allowed_numbers",
        "twilio_validate_signatures",
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
    # Invariant: target_tokens < trigger_tokens <= max_input_tokens.
    if s.context_trim_target_tokens >= s.context_trim_trigger_tokens:
        warnings.append(
            f"context_trim_target_tokens ({s.context_trim_target_tokens})"
            f" >= context_trim_trigger_tokens ({s.context_trim_trigger_tokens});"
            " token-side hysteresis is disabled and compaction may re-fire"
            " on every message after the first overflow"
        )
    if s.context_trim_trigger_tokens > s.max_input_tokens:
        warnings.append(
            f"context_trim_trigger_tokens ({s.context_trim_trigger_tokens})"
            f" > max_input_tokens ({s.max_input_tokens});"
            " trimming will never trigger"
        )
    # Invariant: the turn backstop must be reachable inside the history
    # loader window. A user turn is typically an inbound + outbound row
    # pair, so reaching the trigger takes about 2x its value in rows.
    # When the row cap binds first, facts are still protected by the
    # window-overflow compaction path in load_conversation_history
    # (issue #1427), but that path has less hysteresis than the turn
    # backstop and compacts rows that are still visible to the agent.
    effective_trigger_turns = (
        s.context_trim_trigger_turns
        if s.context_trim_trigger_turns is not None
        else s.context_trim_target_turns + CONTEXT_TRIM_DEFAULT_TRIGGER_BUFFER_TURNS
    )
    if 2 * (effective_trigger_turns + 1) > s.conversation_history_limit:
        warnings.append(
            f"conversation_history_limit ({s.conversation_history_limit}) is below"
            f" 2x the effective turn-trim trigger ({effective_trigger_turns});"
            " the row cap will bind before the turn backstop and old messages"
            " will roll through window-overflow compaction instead"
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

    # Storage moved to per-user Google Drive OAuth. Old deployment-level
    # env vars are silently dropped by Pydantic ``extra='ignore'``; flag
    # them so upgraders notice their config is dead.
    for legacy_key in (
        "STORAGE_PROVIDER",
        "GOOGLE_DRIVE_CREDENTIALS_JSON",
        "FILE_STORAGE_BASE_DIR",
    ):
        if os.environ.get(legacy_key):
            warnings.append(
                f"{legacy_key} is set but no longer supported."
                " File storage is now per-user via Google Drive OAuth; set"
                " GOOGLE_DRIVE_CLIENT_ID + GOOGLE_DRIVE_CLIENT_SECRET and have"
                " each user connect Drive via manage_integration."
            )

    for w in warnings:
        logger.warning("Config: %s", w)

    return warnings
