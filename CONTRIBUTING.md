# Contributing

Clawbolt is open source and contributions are welcome.

## Getting started

1. Fork the repository on [GitHub](https://github.com/mozilla-ai/clawbolt)
2. Clone your fork and set up [local development](#local-setup)
3. Create a branch for your changes
4. Make your changes and ensure all checks pass
5. Open a pull request

## Local setup

For development, you can run Clawbolt directly with Python and uv (no Docker required).

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- PostgreSQL (for data storage)

### Install dependencies

```bash
pip install uv
uv sync
```

### Configure environment

```bash
cp .env.example .env
```

Edit `.env` with your credentials. At minimum:
- An LLM API key
- At least one messaging channel:
  - **iMessage:** pick one backend. Linq (`LINQ_API_TOKEN` + `LINQ_FROM_NUMBER`) for hosted iMessage/RCS/SMS, or BlueBubbles (`BLUEBUBBLES_SERVER_URL` + `BLUEBUBBLES_PASSWORD`) for a self-hosted bridge. The app surfaces either as a single "iMessage" channel to users. Configuring both at once is not supported.
  - **Telegram:** `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_CHAT_ID`

### Set up PostgreSQL

Create the development database:

```bash
createdb -U clawbolt clawbolt
```

Run migrations:

```bash
uv run alembic upgrade head
```

### Start the server

```bash
uv run uvicorn backend.app.main:app --reload
```

The server starts on `http://localhost:8000`.

### File storage in development

The default storage provider is `local`, which saves files to `data/storage/` on disk. No cloud credentials needed.

### Set up a messaging webhook

Without Docker, you need a tunnel to give messaging providers a public URL:

```bash
# Install cloudflared, then:
cloudflared tunnel --url http://localhost:8000
```

If using the **iMessage** channel backed by Linq, the webhook registers automatically when the server detects the tunnel URL. If using BlueBubbles as the iMessage backend, configure the webhook on your BlueBubbles server per its setup guide.

If using **Telegram**, copy the tunnel URL and register the webhook manually:

```bash
curl -X POST "https://api.telegram.org/bot<TOKEN>/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://<tunnel-url>/api/webhooks/telegram"}'
```

See [Linq Setup](./docs/self-host/linq-setup.md), [BlueBubbles Setup](./docs/self-host/bluebubbles-setup.md), or [Telegram Setup](./docs/self-host/telegram-setup.md) for details.

## Testing

Clawbolt uses pytest with FastAPI's TestClient. Tests require a running PostgreSQL instance with a `clawbolt_test` database.

### Database setup

Tests connect to `postgresql://clawbolt:clawbolt@localhost:5432/clawbolt_test`. The conftest handles table creation and per-test transaction rollback automatically.

```bash
# Create the test database (one-time setup)
createdb -U clawbolt clawbolt_test
```

### Run all tests

```bash
uv sync --all-extras
uv run pytest -v
```

### Test infrastructure

**Store isolation:** The `_isolate_file_stores` autouse fixture patches `settings.data_dir` and calls `reset_stores()` to clear cached store singletons between tests. Each test runs in a database transaction that is rolled back after the test completes.

**Mock factories:** All external services are mocked in tests. Mock factories live in `tests/mocks/`:

| Mock | What it replaces |
|------|------------------|
| Telegram | Telegram Bot API calls |
| LLM | any-llm `acompletion` calls |
| Dropbox/Drive | Cloud storage operations |
| Storage | `MockStorageBackend` for file operations |

**Auth override:** The `get_current_user` dependency is overridden in tests to return a fixed test user, bypassing authentication.

## Code standards

- **Type annotations** required on all functions
- **Ruff** for linting and formatting (rules: `E, F, I, UP, B, SIM, ANN, RUF`)
- **Line length**: 100 characters
- **Pydantic v2** for all data classes and request/response schemas
- **Async routes**: all route handlers use `async def`
- **LLM calls**: all LLM calls via any-llm `acompletion` (async)

## Commit messages

Use [conventional commit](https://www.conventionalcommits.org/) prefixes:

| Prefix | Use for |
|--------|---------|
| `feat:` | New features |
| `fix:` | Bug fixes |
| `docs:` | Documentation changes |
| `refactor:` | Code refactoring |
| `test:` | Adding or updating tests |
| `ci:` | CI/CD changes |
| `chore:` | Maintenance tasks |

## Definition of done

Every change should pass all checks:

```bash
uv run pytest -v                                  # tests pass
uv run ruff check backend/ tests/                 # lint passes
uv run ruff format --check backend/ tests/        # format passes
uv run ty check --python .venv backend/ tests/    # type checking passes
```

- Bug fixes include regression tests
- New features include appropriate tests
- Documentation is updated if needed

## Architecture notes

- See [ARCHITECTURE.md](./ARCHITECTURE.md) for the system design overview
- Every data class and endpoint uses `user_id` scoping
- External services are abstracted behind service classes in `backend/app/services/`
- Config uses Pydantic `BaseSettings` with `extra="ignore"`
