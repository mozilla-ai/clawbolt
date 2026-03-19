<p align="center">
  <img src="assets/clawbolt_text.png" alt="clawbolt.ai" width="360">
</p>

<p align="center">
  <strong>AI assistant for the trades</strong><br>
</p>

<p align="center">
  <a href="https://github.com/mozilla-ai/clawbolt/actions/workflows/ci.yml"><img src="https://github.com/mozilla-ai/clawbolt/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/python-3.11+-3776AB?logo=python&logoColor=white" alt="Python 3.11+">
  <a href="https://github.com/mozilla-ai/any-llm"><img src="https://img.shields.io/badge/LLM-any--llm-blueviolet" alt="any-llm"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue" alt="License: Apache 2.0"></a>
  <img src="https://img.shields.io/badge/messaging-Telegram-26A5E4?logo=telegram&logoColor=white" alt="Telegram">
</p>

---

Clawbolt is a messaging-first AI assistant that helps users manage their business: estimates, client records, job photos, voice memos, and more, all through Telegram. No app to install, no dashboard to learn. Just text.

**[Read the full documentation](https://mozilla-ai.github.io/clawbolt)**

## Demo

[![Clawbolt Demo](https://img.youtube.com/vi/YJcnij0SYiY/maxresdefault.jpg)](https://www.youtube.com/watch?v=YJcnij0SYiY)

## Features

- **Memory** -- Clawbolt remembers your rates, clients, preferences, and past conversations
- **Photo analysis** -- Send a job site photo and get an AI description for documentation
- **Voice memos** -- Send a voice note, get it transcribed and processed as a message
- **File cataloging** -- Photos and documents auto-organized in Dropbox or Google Drive
- **Proactive heartbeat** -- Clawbolt checks in periodically with reminders and follow-ups
- **QuickBooks Online** -- Query, create, and send invoices and estimates via QuickBooks (experimental)
- **Onboarding** -- First-time users get a friendly conversation to set up their profile

## Quick Start

```bash
git clone https://github.com/mozilla-ai/clawbolt.git
cd clawbolt
cp .env.example .env
# Edit .env with your TELEGRAM_BOT_TOKEN and LLM API key
docker compose up --build
```

Verify it's running:

```bash
curl http://localhost:8000/api/health
# {"status":"ok"}
```

The Telegram webhook is registered automatically via a Cloudflare Tunnel. Send a message to your bot and Clawbolt will respond.

See the docs for [full configuration options](https://mozilla-ai.github.io/clawbolt/configuration/), [storage setup](https://mozilla-ai.github.io/clawbolt/deployment/storage/), and [Telegram bot setup](https://mozilla-ai.github.io/clawbolt/deployment/telegram-setup/).

## Contributing

See [DEVELOPMENT.md](DEVELOPMENT.md) for local setup and running tests, or read the [full contributing guide](https://mozilla-ai.github.io/clawbolt/development/contributing/).
