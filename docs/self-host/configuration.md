# Configuration

Clawbolt is configured via environment variables. Copy `.env.example` to `.env` and fill in the values.

All available settings are listed in `.env.example` with defaults and comments. This page documents every setting by category.

## Required variables

| Variable | Description |
|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Telegram bot token from [@BotFather](https://t.me/BotFather) |
| `LLM_PROVIDER` | Provider name (any provider supported by [any-llm](https://github.com/mozilla-ai/any-llm)) |
| `LLM_MODEL` | Model identifier for the agent loop (e.g. the model name your provider expects) |
| LLM API key | The API key env var for your chosen provider (e.g. `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`) |
| `TELEGRAM_ALLOWED_CHAT_ID` | Your numeric Telegram user ID. Set to `*` to allow everyone. **Empty = deny all** |

## Core

| Variable | Default | Description |
|----------|---------|-------------|
| `LOG_LEVEL` | `INFO` | Python log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `DATA_DIR` | `data/users` | Directory for file-based user data storage |
| `DATABASE_URL` | `postgresql://clawbolt:clawbolt@localhost:5432/clawbolt` | PostgreSQL connection URL |
| `SETTINGS_STORE` | `db` | Backend for runtime-configurable settings (admin UI values). `db` writes to the `app_settings` table; `file` keeps the legacy `data/config.json` flow for file-based deployments |
| `CORS_ORIGINS` | `http://localhost:3000,http://localhost:8000` | Comma-separated list of allowed CORS origins |
| `JWT_SECRET` | `change-me-in-production` | Secret key for JWT signing. **Change this in production** |
| `JWT_EXPIRY_MINUTES` | `15` | JWT token expiry time in minutes |
| `PREMIUM_PLUGIN` | (empty) | Python import path for premium auth plugin. Leave empty for OSS single-tenant mode |

## LLM configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_PROVIDER` | (required) | LLM provider name (any provider supported by [any-llm](https://github.com/mozilla-ai/any-llm)) |
| `LLM_MODEL` | (required) | Model to use for the agent loop |
| `LLM_API_BASE` | (none) | Custom API base URL (e.g. `http://localhost:1234/v1` for LM Studio) |
| `REASONING_EFFORT` | `auto` | Reasoning effort level: `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, or `auto` |
| `VISION_MODEL` | (same as `LLM_MODEL`) | Model to use for image/document analysis. Falls back to `LLM_MODEL` if not set |
| `VISION_PROVIDER` | (same as `LLM_PROVIDER`) | Provider for the vision model. Falls back to `LLM_PROVIDER` if not set |
| `ANY_LLM_KEY` | | [any-llm.ai](https://any-llm.ai) managed platform key (replaces individual provider keys) |

Set the API key env var for your chosen provider, or set `ANY_LLM_KEY` to use the any-llm.ai managed platform as a key vault for all providers.

## Telegram settings

| Variable | Default | Description |
|----------|---------|-------------|
| `MESSAGING_PROVIDER` | `telegram` | Default messaging backend. Telegram and iMessage channels can run simultaneously; only one iMessage backend (Linq OR BlueBubbles) may be configured at a time |
| `TELEGRAM_BOT_TOKEN` | | Bot token from @BotFather |
| `TELEGRAM_WEBHOOK_SECRET` | (auto-derived) | Webhook validation secret. Auto-derived from bot token if not set |
| `TELEGRAM_ALLOWED_CHAT_ID` | (empty) | Your numeric Telegram user ID, or `*` to allow all. Only a single ID is allowed per user. Empty = deny all |

## iMessage backends

Clawbolt's user-facing "iMessage" channel is powered by one of two interchangeable backends. Choose one: configuring both at once will cause the app to refuse to start. End users see a single "iMessage" option regardless of which backend you pick.

### Linq (hosted iMessage/RCS/SMS)

| Variable | Default | Description |
|----------|---------|-------------|
| `LINQ_API_TOKEN` | | Linq Partner API V3 bearer token |
| `LINQ_FROM_NUMBER` | | Your Linq-provisioned phone number in E.164 format |
| `LINQ_WEBHOOK_SIGNING_SECRET` | | HMAC signing secret from webhook subscription creation |
| `LINQ_ALLOWED_NUMBERS` | (empty) | E.164 phone number, `*` for all, or empty to deny all |
| `LINQ_PREFERRED_SERVICE` | `iMessage` | Preferred messaging service: `iMessage`, `SMS`, or `RCS` |

When `LINQ_API_TOKEN` is set, the iMessage channel is powered by Linq and users can text their assistant from their phone's native messaging app. Linq provides iMessage, RCS, and SMS access via [linqapp.com](https://linqapp.com).

### BlueBubbles (self-hosted iMessage bridge)

| Variable | Default | Description |
|----------|---------|-------------|
| `BLUEBUBBLES_SERVER_URL` | | URL of your BlueBubbles server (e.g. `https://my-mac.ngrok.io`) |
| `BLUEBUBBLES_PASSWORD` | | BlueBubbles server password (used as query param for API auth) |
| `BLUEBUBBLES_ALLOWED_NUMBERS` | (empty) | E.164 phone number or email, `*` for all, or empty to deny all |
| `BLUEBUBBLES_SEND_METHOD` | `apple-script` | Send method: `apple-script` (default) or `private-api` |
| `BLUEBUBBLES_IMESSAGE_ADDRESS` | (empty) | iCloud email or phone number displayed in the UI for users to text |
| `BLUEBUBBLES_BACKFILL_LOOKBACK_MINUTES` | `30` | On startup, ask the BlueBubbles server for messages dated in the last N minutes and replay any whose webhook never reached us during a Clawbolt outage. Idempotency dedup makes this safe on healthy boots. `0` disables the sweep. |
| `BLUEBUBBLES_BACKFILL_INTERVAL_SECONDS` | `300` | Re-run the backfill on this cadence in addition to the boot-time sweep. Catches webhooks lost mid-flight without waiting for a deploy. `0` disables the recurring sweep. |
| `BLUEBUBBLES_HEALTH_CHECK_INTERVAL_SECONDS` | `120` | Re-probe `/api/v1/server/info` on this cadence so the dashboard reachability light reflects current state rather than a snapshot taken at boot. `0` disables the periodic check. |

When `BLUEBUBBLES_SERVER_URL` and `BLUEBUBBLES_PASSWORD` are set, the iMessage channel is powered by BlueBubbles. [BlueBubbles](https://github.com/BlueBubblesApp/bluebubbles-server) is a free, open-source iMessage bridge that runs on any Mac with iMessage signed in.

## Twilio (RCS with SMS/MMS fallback)

The Twilio channel sends through a Messaging Service which can carry an RCS Agent for capable recipients and a phone-number pool for SMS fallback. RCS-capable recipients get rich messaging (branded sender, no character limits, optional read receipts and suggested-reply chips); everyone else receives SMS or MMS, decided by Twilio per recipient.

| Variable | Default | Description |
|----------|---------|-------------|
| `TWILIO_ACCOUNT_SID` | | Twilio account SID (`AC...`) |
| `TWILIO_AUTH_TOKEN` | | Twilio account auth token. Loaded for one purpose only: validating `X-Twilio-Signature` on inbound webhooks. Twilio signs webhooks with HMAC-SHA1 keyed on this token and offers no alternative signing mechanism, so API keys cannot replace it for inbound. |
| `TWILIO_API_KEY_SID` | | Standard API key SID (`SK...`). **Required** for every outbound REST call (send messages, media downloads). The channel refuses outbound work if missing rather than falling back to auth-token Basic Auth, so a leaked auth token cannot be replayed against the REST API. Create via Twilio Console > Account > API Keys & Tokens (Standard key). |
| `TWILIO_API_KEY_SECRET` | | The API key's secret value. Shown once at key creation; record it then. |
| `TWILIO_PHONE_NUMBER` | | E.164 fallback sender for SMS-only deployments, e.g. `+15551234567`. Ignored when `TWILIO_MESSAGING_SERVICE_SID` is set. |
| `TWILIO_MESSAGING_SERVICE_SID` | | Messaging Service SID (`MG...`). Required for RCS; the RCS Agent attaches to the service. When set, takes precedence over `TWILIO_PHONE_NUMBER`. |
| `TWILIO_ALLOWED_NUMBERS` | (empty) | E.164 phone number, `*` for all, or empty to deny all. |
| `TWILIO_VALIDATE_SIGNATURES` | `true` | Validate `X-Twilio-Signature` on inbound webhooks. Leave on in production; turning off is only safe behind a private tunnel during local dev. |

### RCS setup

1. Register your brand and an RCS Agent in the Twilio console. See [Twilio's RCS onboarding guide](https://www.twilio.com/docs/rcs/onboarding) for the brand/agent/carrier-approval workflow (allow 4-6 weeks for the first agent on an account; subsequent agents on an approved brand are faster).
2. Create a Messaging Service and attach the RCS Agent to it. Add any backup SMS sender numbers (toll-free, long-code, or short-code) to the same service so RCS-incapable recipients get SMS without operator intervention.
3. Configure the Messaging Service's inbound webhook (`A MESSAGE COMES IN`) to `https://<your host>/api/webhooks/twilio`, HTTP POST.
4. Set `TWILIO_MESSAGING_SERVICE_SID` to the service SID. Do not set `TWILIO_PHONE_NUMBER`; the service decides the sender per recipient.

Typing indicators are handled by the recipient's messaging app while a reply is in flight; there is no separate API to trigger them, and the channel's `send_typing_indicator` is a no-op.

### SMS-only setup (no RCS)

Skip the agent and Messaging Service. Set `TWILIO_PHONE_NUMBER` to a single E.164 sender and complete the relevant US verification flow (toll-free verification for `+18xx`, A2P 10DLC registration for long-code).

## Google Drive integration

Google Drive is the integration Clawbolt uses for file storage. Each user grants `drive.file` scope through `manage_integration` in chat; files land in the user's own Drive under a top-level "Clawbolt" folder. Without these credentials set, the integration stays unavailable and the file tools (upload, retrieve, analyze) never load for any user.

| Variable | Default | Description |
|----------|---------|-------------|
| `GOOGLE_DRIVE_CLIENT_ID` | | OAuth client ID from the Google Cloud console (with the Drive API enabled) |
| `GOOGLE_DRIVE_CLIENT_SECRET` | | OAuth client secret from the Google Cloud console |

See [Google Drive Setup](./google-drive-setup.md) for the full walkthrough.

## Inbound media staging

Photos and files the user sends over a messaging channel are cached on disk while the agent decides what to do with them (analyze, upload to Drive or CompanyCam, discard). Bytes live on the deployment's filesystem under `MEDIA_STAGING_BASE_DIR`; metadata (handle, original URL, mime type, expiry) lives in the `staged_media` Postgres table. The cache holds each item for 7 days so the agent can still reference photos from earlier in the week.

| Variable | Default | Description |
|----------|---------|-------------|
| `MEDIA_STAGING_BASE_DIR` | `data/staged_media` | Filesystem directory for staged media bytes. Point at a persistent volume in production so a process restart does not discard recent inbound media. The single application instance is assumed to own this path exclusively; multi-replica deployments are not currently supported. |

## LLM token limits

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_MAX_TOKENS_AGENT` | `500` | Max tokens per agent loop LLM response |
| `LLM_MAX_TOKENS_HEARTBEAT` | `300` | Max tokens per heartbeat LLM response |
| `LLM_MAX_TOKENS_VISION` | `1000` | Max tokens per vision/image analysis response |

## Agent loop

| Variable | Default | Description |
|----------|---------|-------------|
| `APPROVAL_TIMEOUT_SECONDS` | `120` | Seconds to wait for user approval of a tool call before automatically denying |
| `AGENT_PROCESSING_TIMEOUT_SECONDS` | `300` | Maximum seconds for a single message's agent processing (includes waiting for the per-user lock). Prevents one hung LLM call from blocking all subsequent messages for the same user |
| `MESSAGE_BATCH_WINDOW_MS` | `1500` | Milliseconds to wait for more messages before processing. Groups rapid-fire messages into one agent call. Set to `0` to disable |
| `INBOUND_RECOVERY_LOOKBACK_MINUTES` | `30` | On startup, sweep for inbound messages persisted but never dispatched to the agent (worker died during the batcher window). Re-dispatch each one. Older orphans are skipped. Set to `0` to disable |
| `MAX_TOOL_ROUNDS` | `10` | Maximum tool-calling rounds per agent invocation |
| `MAX_INPUT_TOKENS` | `120000` | Max input token budget before context trimming |
| `CONTEXT_TRIM_TARGET_TOKENS` | `80000` | Target token count after trimming |
| `CONTEXT_TRIM_TARGET_TURNS` | `80` | Cap on user turns kept verbatim in LLM context. Long single-conversation histories reinforce their own dominant tone, so this trims oldest turns past the cap (independent of the token budget) and rolls them through compaction into `MEMORY.md`, `USER.md`, and `SOUL.md` |
| `CONTEXT_TRIM_TRIGGER_TURNS` | unset (defaults to `CONTEXT_TRIM_TARGET_TURNS + 16`) | Trim fires when user-turn count exceeds this threshold and drops down to `CONTEXT_TRIM_TARGET_TURNS`, leaving headroom before the next trim. Without this hysteresis (single threshold), the resting state sits exactly at the cap and every subsequent user message re-fires trim plus the downstream compaction LLM call. Set to `None` (omit the env var entirely) for the default 16-turn buffer |
| `COMPACTION_EVENT_SNAPSHOT_MAX_BYTES_PER_FILE` | `100000` | Per-file truncation cap for memory-text snapshots persisted on `compaction_events` rows. When `MEMORY.md` / `HISTORY.md` / `USER.md` / `SOUL.md` exceeds this size, the snapshot column stores a structured truncation record (head, tail, size, sha256) so admin diff visibility is preserved while bounding worst-case row size |
| `LLM_MAX_RETRIES` | `3` | Maximum number of retry attempts on rate limit errors |
| `LLM_CACHE_EXTENDED_TTL` | `true` | Use Anthropic's 1-hour extended cache TTL instead of the default 5 minutes. Reduces cold-start cache misses for users with multi-hour gaps between messages. Set to `false` on non-Anthropic providers that reject the `ttl` field |

## Conversation and memory

| Variable | Default | Description |
|----------|---------|-------------|
| `CONVERSATION_HISTORY_LIMIT` | `20` | Max messages included in LLM context |
| `MEMORY_RECALL_LIMIT` | `20` | Max memory facts recalled per query |
| `COMPACTION_ENABLED` | `true` | Enable automatic conversation compaction |
| `COMPACTION_MODEL` | (same as `LLM_MODEL`) | Model used for compaction |
| `COMPACTION_PROVIDER` | (same as `LLM_PROVIDER`) | Provider used for compaction |
| `COMPACTION_MAX_TOKENS` | `500` | Max tokens per compaction response |

## Rate limiting

| Variable | Default | Description |
|----------|---------|-------------|
| `WEBHOOK_RATE_LIMIT_MAX_REQUESTS` | `30` | Max webhook requests per window |
| `WEBHOOK_RATE_LIMIT_WINDOW_SECONDS` | `60` | Rate limit window in seconds |
| `RATE_LIMIT_TRUST_PROXY` | `false` | Trust `X-Forwarded-For` for client IP (set `true` behind a reverse proxy) |
| `UNKNOWN_SENDER_SIGNUP_URL` | (empty) | Sign-up link included in the reply we send to numbers not in the allowlist. Empty = generic copy pointing at clawbolt.ai |
| `UNKNOWN_SENDER_REPLY_COOLDOWN_SECONDS` | `86400` | Minimum seconds between unknown-sender replies to the same `(channel, sender)`. Prevents the bot from being used as a spam relay |

## Heartbeat (proactive check-ins)

| Variable | Default | Description |
|----------|---------|-------------|
| `HEARTBEAT_ENABLED` | `true` | Enable proactive check-in messages |
| `HEARTBEAT_DEFAULT_FREQUENCY` | `30m` | Default check-in frequency shown to new users (e.g. `15m`, `1h`, `daily`) |
| `HEARTBEAT_INTERVAL_MINUTES` | `30` | Minutes between heartbeat evaluation ticks |
| `HEARTBEAT_MAX_DAILY_MESSAGES` | `5` | Max proactive messages per user per day |
| `HEARTBEAT_MODEL` | (same as `LLM_MODEL`) | Model used for heartbeat messages |
| `HEARTBEAT_PROVIDER` | (same as `LLM_PROVIDER`) | Provider used for heartbeat messages |
| `HEARTBEAT_CONCURRENCY` | `5` | Max concurrent user evaluations per tick |
| `HEARTBEAT_RECENT_MESSAGES_COUNT` | `5` | Number of recent messages included in heartbeat context |
| `HEARTBEAT_USER_QUIET_PERIOD_MINUTES` | `5` | Minutes since the user's last message during which the heartbeat LLM call is skipped, to avoid burning tokens on "skip" decisions during an active conversation. Set to `0` to disable. |
| `HEARTBEAT_STARTUP_WARMUP_SECONDS` | `60` | Seconds the scheduler sleeps before its first tick after process start, so post-deploy in-flight work and queued inbound messages can drain. Set to `0` to disable. |

## Observability

| Variable | Default | Description |
|----------|---------|-------------|
| `LOG_REQUEST_TIMING` | `false` | Log method, path, status code, and duration for every HTTP request |

## OAuth

| Variable | Default | Description |
|----------|---------|-------------|
| `APP_BASE_URL` | `http://localhost:8000` | Public URL where the server is reachable, used for OAuth callback URLs |
| `ENCRYPTION_KEY` | | Encrypts OAuth tokens at rest in the database. Generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Required for production. |

## QuickBooks Online

| Variable | Default | Description |
|----------|---------|-------------|
| `QUICKBOOKS_CLIENT_ID` | | OAuth client ID from the Intuit developer console |
| `QUICKBOOKS_CLIENT_SECRET` | | OAuth client secret from the Intuit developer console |
| `QUICKBOOKS_ENVIRONMENT` | `sandbox` | `sandbox` for testing or `production` for live data |

When `QUICKBOOKS_CLIENT_ID` and `QUICKBOOKS_CLIENT_SECRET` are set, users can connect their QuickBooks account via the web dashboard. Once connected, the agent gains specialist QuickBooks tools (`qb_query`, `qb_create`, `qb_update`, `qb_send`).

## Google Calendar

| Variable | Default | Description |
|----------|---------|-------------|
| `GOOGLE_CALENDAR_CLIENT_ID` | | OAuth client ID from the Google Cloud console |
| `GOOGLE_CALENDAR_CLIENT_SECRET` | | OAuth client secret from the Google Cloud console |

When `GOOGLE_CALENDAR_CLIENT_ID` and `GOOGLE_CALENDAR_CLIENT_SECRET` are set, users can connect their Google Calendar via the web dashboard. Once connected, the agent gains specialist calendar tools (`calendar_list_events`, `calendar_create_event`, `calendar_update_event`, `calendar_delete_event`, `calendar_check_availability`).

## Gmail

| Variable | Default | Description |
|----------|---------|-------------|
| `GMAIL_CLIENT_ID` | | OAuth client ID from the Google Cloud console |
| `GMAIL_CLIENT_SECRET` | | OAuth client secret from the Google Cloud console |

Use a separate Google OAuth client from Calendar/Drive so the `gmail.readonly` and `gmail.send` scopes can be approved on their own (Google's verification process treats each OAuth client independently). When both variables are set, users can connect Gmail via `manage_integration(action='connect', target='gmail')` or the Tools page. Once connected, the agent gains four tools: `gmail_search`, `gmail_get_message`, `gmail_list_recent`, and `gmail_send`. All four default to `ask` permission so the user is prompted before any inbox read or outbound send.

## CompanyCam

| Variable | Default | Description |
|----------|---------|-------------|
| `COMPANYCAM_CLIENT_ID` | | CompanyCam OAuth 2.0 client ID. Register at [docs.companycam.com/docs/oauth](https://docs.companycam.com/docs/oauth) |
| `COMPANYCAM_CLIENT_SECRET` | | CompanyCam OAuth 2.0 client secret |
| `COMPANYCAM_WEB_BASE` | `https://app.companycam.com` | Web app base URL used to build clickable receipt deep links. Override only if CompanyCam ships a non-US host. |

When both are set, users can connect CompanyCam via OAuth on the Tools page or through chat. This enables tools like `companycam_search_projects`, `companycam_upload_photo`, and `companycam_create_project`.

## AppFolio Vendor Portal

| Variable | Default | Description |
|----------|---------|-------------|
| `APPFOLIO_VENDOR_API_BASE` | `https://vendor.appf.io` | Base URL of the AppFolio Vendor Portal API. Override only for staging or test environments. |
| `APPFOLIO_VENDOR_WEB_BASE` | `https://vendor.appfolio.com` | Web base shown to users when prompting them to request a magic link. |

The integration uses passwordless magic-link auth: users paste the URL from their AppFolio email and the agent exchanges it for a Bearer JWT. No client ID or secret to configure. Once connected, the agent gains tools like `appfolio_list_work_orders`, `appfolio_search_work_orders`, and `appfolio_list_payments`.

## ServiceTitan

| Variable | Default | Description |
|----------|---------|-------------|
| `SERVICETITAN_APP_KEY` | | App-level App Key issued by ServiceTitan to the integrator. Sent as the `ST-App-Key` header on every API call. |
| `SERVICETITAN_API_BASE_URL` | `https://api.servicetitan.io` | Base URL of the ServiceTitan resource API (customers, jobs, appointments). Override to `https://api-integration.servicetitan.io` for the integration sandbox. |
| `SERVICETITAN_AUTH_BASE_URL` | `https://auth.servicetitan.io` | Base URL of the ServiceTitan auth host that serves `/connect/token`. Distinct from the resource host. Override to `https://auth-integration.servicetitan.io` for the integration sandbox; flip both `API_BASE_URL` and `AUTH_BASE_URL` together when switching environments. |
| `SERVICETITAN_USE_FAKE` | `true` | When true, route every call through the in-process fake backend (deterministic seed data, no real network). Flip to false once a real tenant is available. |

ServiceTitan uses OAuth 2.0 client credentials (machine-to-machine), one set per tenant. The operator wires the integrator's app-level App Key here; each tenant pastes their own tenant ID, client ID, and client secret through the `connect_servicetitan` tool in chat. Stored credentials are envelope-encrypted at rest. ServiceTitan splits auth and resource traffic across two hosts, so `AUTH_BASE_URL` and `API_BASE_URL` are independent settings.

## Supplier pricing

| Variable | Default | Description |
|----------|---------|-------------|
| `SERPAPI_API_KEY` | | SerpApi API key for Home Depot product price lookups. Free tier: 250 searches/month at [serpapi.com](https://serpapi.com) |

When `SERPAPI_API_KEY` is set, the agent gains a `supplier_search_products` specialist tool that looks up Home Depot product prices, ratings, and links by keyword and zip code.

## HTTP timeouts

| Variable | Default | Description |
|----------|---------|-------------|
| `HTTP_TIMEOUT_SECONDS` | `30.0` | Default timeout for outbound HTTP requests |
| `CLOUDFLARED_METRICS_TIMEOUT_SECONDS` | `5.0` | Timeout for cloudflared tunnel metrics check |
| `TELEGRAM_WEBHOOK_TIMEOUT_SECONDS` | `10.0` | Timeout for Telegram webhook registration |

## Media

| Variable | Default | Description |
|----------|---------|-------------|
| `MAX_MEDIA_SIZE_BYTES` | `20971520` | Max upload size (20 MB default) |
| `MEDIA_DOWNLOAD_MAX_SECONDS` | `60.0` | Hard wall-time ceiling per media download (slow-drip guard) |
