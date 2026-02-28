# Development

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
