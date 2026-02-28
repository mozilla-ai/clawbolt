# Backshop

AI assistant for solo blue-collar contractors. Built by Mozilla.ai.

Backshop is an SMS-first AI assistant that helps contractors manage their business — estimates, client records, job photos, voice memos, and more — all through text messages.

## Quick Start (Docker)

The fastest way to run the demo is with Docker Compose. This starts PostgreSQL and the app together.

### 1. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and fill in the required credentials:

| Variable | Required | Description |
|----------|----------|-------------|
| `LLM_API_KEY` | Yes | API key for your LLM provider (OpenAI, Anthropic, etc.) |
| `TWILIO_ACCOUNT_SID` | Yes | Twilio account SID |
| `TWILIO_AUTH_TOKEN` | Yes | Twilio auth token |
| `TWILIO_PHONE_NUMBER` | Yes | Your Twilio phone number (e.g. `+15551234567`) |
| `LLM_PROVIDER` | No | LLM provider name (default: `openai`) |
| `LLM_MODEL` | No | Model to use (default: `gpt-4o`) |
| `STORAGE_PROVIDER` | No | `dropbox` or `google_drive` for file cataloging |
| `DROPBOX_ACCESS_TOKEN` | No | Dropbox token (if using Dropbox storage) |

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

### 4. Expose to Twilio (for SMS)

Twilio needs a public URL to send webhooks. Use a tunnel service:

```bash
# Using ngrok
ngrok http 8000

# Or using Cloudflare Tunnel
cloudflared tunnel --url http://localhost:8000
```

Then configure your Twilio phone number's webhook URL to:

```
https://<your-tunnel-url>/api/webhooks/twilio/inbound
```

Set the HTTP method to **POST**.

### 5. Test it

Send a text message to your Twilio number. Backshop will respond as an AI assistant ready to help with estimates, job tracking, and more.

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

### Twilio webhooks not arriving

1. Verify your tunnel is running and the URL is accessible
2. Check that `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN` are set correctly in `.env`
3. Set `TWILIO_VALIDATE_SIGNATURES=false` in `.env` during local development if signature validation is failing (the tunnel URL must match exactly)
4. Check the Twilio console for webhook delivery errors

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
