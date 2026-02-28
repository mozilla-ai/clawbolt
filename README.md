# Backshop

AI assistant for solo blue-collar contractors. Built by Mozilla.ai.

Backshop is a messaging-first AI assistant that helps contractors manage their business — estimates, client records, job photos, voice memos, and more — all through Telegram.

## Quick Start (Docker)

The fastest way to run the demo is with Docker Compose. This starts PostgreSQL and the app together.

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
| `STORAGE_PROVIDER` | No | `dropbox` or `google_drive` for file cataloging |
| `DROPBOX_ACCESS_TOKEN` | No | Dropbox token (if using Dropbox storage) |
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

## Local Development (without Docker)

```bash
pip install uv
uv sync
uv run uvicorn backend.app.main:app --reload
```

You'll need a PostgreSQL instance running locally, or set `DATABASE_URL` accordingly.

## Running Tests

```bash
uv sync --all-extras
uv run pytest -v
uv run ruff check backend/ tests/
uv run ruff format --check backend/ tests/
```

Tests use in-memory SQLite — no database setup needed.

## Troubleshooting

### Docker build fails with dependency errors

Some optional dependencies (e.g. `faster-whisper`) require specific system libraries. The Dockerfile includes `ffmpeg` for audio processing. If you see build failures:

```bash
# Rebuild without cache
docker compose build --no-cache
```

### Database connection refused

Make sure PostgreSQL is healthy before the app starts. Docker Compose handles this via the `service_healthy` condition, but if you see connection errors:

```bash
# Check service status
docker compose ps

# View logs
docker compose logs db
docker compose logs app
```

### Telegram webhook not receiving messages

1. Verify your tunnel is running and the URL is accessible
2. Check that `TELEGRAM_BOT_TOKEN` is set correctly in `.env`
3. Verify the webhook is set: `curl https://api.telegram.org/bot<TOKEN>/getWebhookInfo`
4. Check the Telegram Bot API response for errors

### Port 8000 already in use

```bash
# Stop existing containers
docker compose down

# Or use a different port
docker compose up --build -e APP_PORT=8080
```

### Reset the database

```bash
docker compose down -v   # removes the pgdata volume
docker compose up --build
```
