# Docker

Docker Compose is the recommended way to run Clawbolt. It handles the app and tunnel setup.

## Quick start

```bash
# Clone and configure
git clone https://github.com/mozilla-ai/clawbolt.git
cd clawbolt
cp .env.example .env
# Edit .env with your credentials

# Start everything
docker compose up --build
```

## What Docker Compose starts

| Service | Description |
|---------|-------------|
| **app** | FastAPI server on port 8000 |
| **db** | PostgreSQL database for all structured data |
| **tunnel** | Cloudflare Tunnel for HTTPS webhook registration |

On startup, the app:
1. Runs database migrations automatically
2. Starts the FastAPI server
3. Auto-registers webhooks via the Cloudflare Tunnel URL (Telegram and, if Linq is the configured iMessage backend, Linq)

## Data persistence

Structured data (users, messages, memory, etc.) is stored in PostgreSQL. The Docker Compose file includes a PostgreSQL service with a named volume for persistence.

The `data/` bind mount holds staged inbound media and per-user workspace files. Saved user files live in each user's Google Drive.

## Verify it's running

```bash
curl http://localhost:8000/api/health
# {"status":"ok"}
```

## Troubleshooting

### Docker build fails with dependency errors

Try rebuilding without cache:

```bash
# Rebuild without cache
docker compose build --no-cache
```

### Port 8000 already in use

```bash
# Stop existing containers
docker compose down

# Or change the host side of the app mapping in docker-compose.yml,
# for example "8080:8000", then start again
docker compose up --build
```
