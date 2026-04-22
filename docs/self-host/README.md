# Self-hosting Clawbolt

Clawbolt is an open-source AI assistant for the trades. This directory covers running your own instance: configuration, channel setup, storage, and deployment.

For end-user documentation (how to use the assistant), see [clawbolt.ai/docs](https://clawbolt.ai/docs).

## Quickstart

The fastest way to run Clawbolt is with Docker Compose.

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and Docker Compose
- A messaging channel: an iMessage backend ([Linq](./linq-setup.md) for hosted iMessage/RCS/SMS, or [BlueBubbles](./bluebubbles-setup.md) for self-hosted iMessage) or a [Telegram bot](./telegram-setup.md)
- An LLM provider API key (OpenAI, Anthropic, etc.)

### 1. Clone the repository

```bash
git clone https://github.com/mozilla-ai/clawbolt.git
cd clawbolt
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and fill in the required credentials. See [Configuration](./configuration.md) for the full list.

At minimum you need:
- An LLM API key (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, etc.)
- `VISION_MODEL` (the model used for image analysis, defaults to `LLM_MODEL` if not set)
- At least one messaging channel configured:
  - **iMessage (recommended):** choose one backend. Hosted iMessage/RCS/SMS via [Linq](./linq-setup.md) (`LINQ_API_TOKEN` + `LINQ_FROM_NUMBER`), or self-hosted iMessage via [BlueBubbles](./bluebubbles-setup.md) (`BLUEBUBBLES_SERVER_URL` + `BLUEBUBBLES_PASSWORD`). The app surfaces whichever backend you configure as a single "iMessage" channel to end users. Configuring both at once is not supported.
  - **Telegram:** `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_CHAT_ID`, see [Telegram Setup](./telegram-setup.md)

### 3. Start the services

```bash
docker compose up --build
```

This will:
- Start PostgreSQL for data storage
- Build the app image (Python 3.11, ffmpeg for audio processing)
- Run database migrations automatically
- Start the FastAPI server on port 8000

### 4. Verify it's running

```bash
curl http://localhost:8000/api/health
# {"status":"ok"}
```

### 5. Start chatting

Docker Compose starts a [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) alongside the app and registers webhooks automatically. No Cloudflare account or auth token required.

Send a message to your assistant and Clawbolt will respond. If you configured an iMessage backend, send an iMessage (or SMS/RCS if Linq is your backend) to the configured phone number or address. If you configured Telegram, message your bot on Telegram.

## Next steps

- [Configuration](./configuration.md) -- full list of environment variables
- [Docker](./docker.md) -- Docker Compose details and troubleshooting
- [Storage Providers](./storage.md) -- configure Dropbox, Google Drive, or local file storage
- [Architecture](../../ARCHITECTURE.md) -- how Clawbolt is built
- [Contributing](../../CONTRIBUTING.md) -- local development setup, tests, and PR guidelines
