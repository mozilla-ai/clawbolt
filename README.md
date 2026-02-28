<p align="center">
  <img src="assets/logo.svg" alt="backshop.ai" width="360">
</p>

<p align="center">
  <strong>AI assistant for solo blue-collar contractors</strong><br>
  Built by <a href="https://mozilla.ai">Mozilla.ai</a>
</p>

<p align="center">
  <a href="https://github.com/njbrake/backshop/actions/workflows/ci.yml"><img src="https://github.com/njbrake/backshop/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/python-3.11+-3776AB?logo=python&logoColor=white" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="License: MIT">
  <img src="https://img.shields.io/badge/messaging-Telegram-26A5E4?logo=telegram&logoColor=white" alt="Telegram">
</p>

---

Backshop is a messaging-first AI assistant that helps contractors manage their business — estimates, client records, job photos, voice memos, and more — all through Telegram. No app to install, no dashboard to learn. Just text.

## Features

- **Estimates** — Describe a job, get a professional PDF estimate generated and sent back instantly
- **Memory** — Backshop remembers your rates, clients, preferences, and past conversations
- **Photo analysis** — Send a job site photo and get an AI description for documentation
- **Voice memos** — Send a voice note, get it transcribed and processed as a message
- **File cataloging** — Photos and documents auto-organized in Dropbox or Google Drive
- **Proactive heartbeat** — Backshop checks in periodically with reminders about stale drafts and follow-ups
- **Onboarding** — First-time contractors get a friendly conversation to set up their profile

## How It Works

```
Contractor sends a Telegram message
        |
        v
  Telegram webhook  -->  Media pipeline (photos, voice, docs)
        |                        |
        v                        v
  Agent loop (any-llm)  <--  Processed context
        |
        v
  Tool execution (memory, estimates, file upload)
        |
        v
  Reply sent via Telegram
```

The agent uses an LLM (configurable via [any-llm](https://github.com/mozilla-ai/any-llm)) to understand messages, call tools, and generate replies. A background heartbeat evaluates each contractor periodically and sends proactive messages when genuinely useful.

## Quick Start (Docker)

The fastest way to run Backshop is with Docker Compose. This starts PostgreSQL and the app together.

### 1. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and fill in the required credentials:

| Variable | Required | Description |
|----------|----------|-------------|
| `OPENAI_API_KEY` | Yes* | OpenAI API key (or set the key for your chosen provider) |
| `TELEGRAM_BOT_TOKEN` | Yes | Telegram bot token from @BotFather |
| `LLM_PROVIDER` | No | LLM provider name (default: `openai`) |
| `LLM_MODEL` | No | Model to use (default: `gpt-4o`) |
| `STORAGE_PROVIDER` | No | `local` (default), `dropbox`, or `google_drive` for file cataloging |
| `DROPBOX_ACCESS_TOKEN` | No | Dropbox token (if using Dropbox storage) |
| `TELEGRAM_ALLOWED_CHAT_IDS` | No | Comma-separated allowlist of Telegram chat IDs (empty = allow all) |
| `TELEGRAM_ALLOWED_USERNAMES` | No | Comma-separated allowlist of Telegram usernames (empty = allow all) |
| `ANY_LLM_KEY` | No | any-llm.ai managed platform key (replaces individual provider keys) |

*Set the API key env var for your chosen provider (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, etc.) — or set `ANY_LLM_KEY` to use the [any-llm.ai](https://any-llm.ai) managed platform as a key vault for all providers.

The `DATABASE_URL` is set automatically by Docker Compose — you don't need to change it.

### 2. Start the services

```bash
docker compose up --build
```

This will:
- Start a PostgreSQL 16 database
- Build the app image (Python 3.11, ffmpeg for audio processing)
- Run Alembic migrations to create the database schema
- Start the FastAPI server on port 8000

### 3. Verify it's running

```bash
curl http://localhost:8000/api/health
# {"status":"ok"}
```

### 4. Set up Telegram webhook

Create a bot via [@BotFather](https://t.me/BotFather) on Telegram. Then expose your server and set the webhook:

```bash
# Using ngrok
ngrok http 8000

# Set the webhook URL (replace with your tunnel URL and bot token)
curl -X POST "https://api.telegram.org/bot<YOUR_BOT_TOKEN>/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://<your-tunnel-url>/api/webhooks/telegram", "secret_token": "<optional-secret>"}'
```

If you set a `secret_token`, also set `TELEGRAM_WEBHOOK_SECRET` in your `.env`.

### 5. Test it

Send a message to your bot on Telegram. Backshop will respond as an AI assistant ready to help with estimates, job tracking, and more.

## File Storage Setup

Backshop can catalog job photos, estimates, and documents to a storage backend. Three providers are supported:

### Local (default)

Works out of the box — no configuration needed. Files are saved to `data/storage/` on disk.

```
data/storage/
├── Job Photos/
│   └── 2026-02-28/
│       ├── site-front.jpg
│       └── site-back.jpg
└── Estimates/
    └── EST-001.pdf
```

This is ideal for development and demos. Set `STORAGE_PROVIDER=local` (or leave it unset — it's the default).

### Dropbox

1. Go to the [Dropbox App Console](https://www.dropbox.com/developers/apps) and create a new app
2. Choose **Scoped access** and **Full Dropbox** (or **App folder** for sandboxed access)
3. Under **Permissions**, enable: `files.content.write`, `files.content.read`, `sharing.write`, `sharing.read`
4. Generate an **access token** on the app's settings page
5. Set environment variables:

```bash
STORAGE_PROVIDER=dropbox
DROPBOX_ACCESS_TOKEN=sl.xxxxx...
```

### Google Drive

1. Go to the [Google Cloud Console](https://console.cloud.google.com/) and create a project
2. Enable the **Google Drive API**
3. Create **OAuth 2.0 credentials** (Desktop app type)
4. Complete the OAuth flow to get a credentials JSON
5. Set environment variables:

```bash
STORAGE_PROVIDER=google_drive
GOOGLE_DRIVE_CREDENTIALS_JSON='{"token": "...", "refresh_token": "...", ...}'
```

> **Multi-tenant note**: Storage is currently global — one storage account per deployment. Future versions will support per-contractor storage credentials so each contractor's files go to their own cloud account.

## Contributing

See [DEVELOPMENT.md](DEVELOPMENT.md) for local setup, running tests, and troubleshooting.
