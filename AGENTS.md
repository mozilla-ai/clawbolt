# Clawbolt

Clawbolt is an AI assistant for the trades. FastAPI backend with a Telegram messaging interface and a custom tool-calling agent loop built on any-llm. Built by Mozilla.ai using the open-core model.

## Build & Run Commands

```bash
# Install dependencies
uv sync

# Run server (requires PostgreSQL -- see docker-compose.yml)
uv run uvicorn backend.app.main:app --reload

# Run with Docker (starts Postgres + app, runs migrations automatically)
docker compose up

# Database migrations
uv run alembic upgrade head
uv run alembic revision --autogenerate -m "description"

# Tests
uv run pytest -v

# Lint & format
uv run ruff check backend/ tests/ alembic/
uv run ruff format --check backend/ tests/ alembic/

# Type checking
uv run ty check --python .venv backend/ tests/ alembic/
```

## Tech Stack

- Python 3.11+, FastAPI, SQLAlchemy 2.0, Pydantic v2
- any-llm-sdk (LLM provider abstraction via `acompletion`)
- Telegram Bot API for messaging (via python-telegram-bot), faster-whisper for audio transcription
- Dropbox/Google Drive for file storage
- PostgreSQL for all data persistence, Alembic for migrations
- uv + hatchling build system, ruff linting, ty type checking

## Storage

All structured data is stored in PostgreSQL (configurable via `DATABASE_URL`). The database has 12 tables:

| Table | Purpose |
|---|---|
| `users` | User profiles, personality text, preferences |
| `channel_routes` | Channel -> user routing (Telegram, webchat, etc.) |
| `sessions` | Chat session metadata |
| `messages` | Chat messages (FK to sessions) |
| `media_files` | Media file manifest |
| `memory_documents` | Structured memory and compaction history |
| `heartbeat_logs` | Heartbeat send log |
| `idempotency_keys` | Webhook deduplication |
| `llm_usage_logs` | Token usage tracking |
| `tool_configs` | Per-user tool configuration |
| `calendar_configs` | Per-user calendar integration settings |
| `oauth_tokens` | Encrypted OAuth tokens for integrations (Google Calendar, QuickBooks) |

Key store modules:
- `backend/app/agent/user_db.py` -- `UserStore` (singleton via `get_user_store()`)
- `backend/app/agent/session_db.py` -- `SessionStore` (per-user via `get_session_store(id)`)
- `backend/app/agent/memory_db.py` -- `MemoryStore` (per-user via `get_memory_store(id)`)
- `backend/app/agent/stores.py` -- `MediaStore`, `HeartbeatStore`, `IdempotencyStore`, `LLMUsageStore`, `ToolConfigStore`
- `backend/app/agent/dto.py` -- Pydantic DTOs: `UserData`, `StoredMessage`, `SessionState`, etc.
- `backend/app/agent/file_store.py` -- Compatibility shim (re-exports from above modules)
- `backend/app/database.py` -- `Base`, `SessionLocal`, `get_db()`, `get_engine()`
- `backend/app/models.py` -- All SQLAlchemy ORM model classes

File storage for uploads uses the local filesystem under `data/` (configurable via `DATA_DIR`).

## Backwards Compatibility

Until this project has its first production release, you do not need to be concerned about backwards compatible changes.

## Coding Standards

- All type annotations required
- Ruff rules: `E, F, I, UP, B, SIM, ANN, RUF` (line length 100, `E501` and `B008` ignored)
- SQLAlchemy 2.0 `mapped_column` style for all ORM models
- Pydantic v2 for all data classes and request/response schemas
- All routes `async def`
- All LLM calls via any-llm `acompletion` (async)
- Never use `BaseHTTPMiddleware` for streaming endpoints -- use pure ASGI middleware
- Conventional commit prefixes: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `ci:`, `chore:`
- Every data endpoint uses `Depends(get_current_user)` with `user_id` scoping
- Config via Pydantic `BaseSettings` with `extra="ignore"`
- Never use em dashes in user-facing content, comments, or copy -- use periods, commas, colons, or pipes instead
- All imports at the top of the file. No inline or deferred imports inside functions. The only exception is `TYPE_CHECKING` guarded imports.

## Testing

- pytest with FastAPI `TestClient`
- PostgreSQL for all tests (requires a local `clawbolt_test` database; see conftest.py)
- `reset_stores()` clears cached store singletons between tests
- Override `get_current_user` via FastAPI dependency injection
- Mock ALL external services: Telegram, LLM (any-llm), faster-whisper, Dropbox/Drive
- Bug fixes must include regression tests

## Architecture

- **PostgreSQL storage**: all structured data in PostgreSQL via SQLAlchemy 2.0 ORM. See `backend/app/database.py` and `backend/app/models.py`. Store modules in `backend/app/agent/` provide CRUD APIs.
- **Auth plugin infrastructure**: base.py (ABC), loader.py (dynamic import), dependencies.py (get_current_user), scoping.py (row-level auth). OSS is single-tenant; premium adds multi-tenant auth via plugin.
- **`user_id` scoping** on every data class and endpoint from day one
- **Message bus**: async inbound/outbound queues in `bus.py`. Channels publish inbound messages; the agent publishes outbound replies. The ``ChannelManager`` dispatches outbound messages to the correct channel.
- **Agent loop**: Telegram webhook -> media pipeline -> tool-calling loop (any-llm `acompletion`) -> tool execution -> reply
- **Memory**: Freeform per-user MEMORY.md managed via workspace tools, backed by `memory_documents` table with automatic compaction
- **Services**: External services abstracted behind service classes in `backend/app/services/`

## Definition of Done

Every change must pass all checks before it's considered complete:

```bash
uv run pytest -v                                  # tests pass
uv run ruff check backend/ tests/ alembic/                 # lint passes
uv run ruff format --check backend/ tests/ alembic/        # format passes
uv run ty check --python .venv backend/ tests/ alembic/    # type checking passes
cd frontend && npm run typecheck                   # TypeScript type checking passes
cd frontend && npm run deadcode                    # no dead JS/TS code (knip)
```

### Frontend generated types

When backend schemas change (`backend/app/schemas.py`, route signatures, or response models), you **must** regenerate the frontend OpenAPI types. Never hand-edit `frontend/src/generated/api.d.ts`. CI will fail if the committed file doesn't match what the generator produces.

```bash
uv run python scripts/export_openapi.py           # export openapi.json from backend
cd frontend && npm run generate:api                # regenerate src/generated/api.d.ts
```

Commit both `frontend/openapi.json` and `frontend/src/generated/api.d.ts`.

- Bug fixes include regression tests
- New features evaluate whether the docs site (`docs/`) needs updates
- When you manage a pull request, you must always adhere to the pull request template at .github/pull_request_template.md
- CI green

## Sandbox Tips

### Ephemeral directories

`target/`, `node_modules/`, and `.venv/` don't persist between sessions. Run `uv sync` at the start of each session if needed.

### PostgreSQL for tests

Tests require a running PostgreSQL instance. In a sandbox without Docker, install and start PostgreSQL directly:

```bash
# Install PostgreSQL (Debian/Ubuntu)
apt-get update -qq && apt-get install -y -qq postgresql postgresql-client

# Start the cluster
pg_ctlcluster 16 main start

# Create the test user and database
su - postgres -c "psql -c \"CREATE USER clawbolt WITH PASSWORD 'clawbolt' CREATEDB;\""
su - postgres -c "psql -c \"CREATE DATABASE clawbolt_test OWNER clawbolt;\""
```

The test suite connects to `postgresql://clawbolt:clawbolt@localhost:5432/clawbolt_test`. The conftest.py handles table creation and per-test transaction rollback automatically.

### Git operations

Git auth is pre-configured. Never push directly to main. Always create a branch and open a PR.

## Design System
Always read DESIGN.md before making any visual or UI decisions.
All font choices, colors, spacing, and aesthetic direction are defined there.
Do not deviate without explicit user approval.
In QA mode, flag any code that doesn't match DESIGN.md.
