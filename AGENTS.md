# Clawbolt

Clawbolt is an AI assistant for the trades. FastAPI backend with a Telegram messaging interface and a custom tool-calling agent loop built on any-llm. Built by Mozilla.ai using the open-core model.

## Build & Run Commands

```bash
# Install dependencies
uv sync

# Run server
uv run uvicorn backend.app.main:app --reload

# Tests
uv run pytest -v

# Lint & format
uv run ruff check backend/ tests/
uv run ruff format --check backend/ tests/

# Type checking
uv run ty check --python .venv backend/ tests/
```

## Tech Stack

- Python 3.11+, FastAPI, Pydantic v2
- any-llm-sdk (LLM provider abstraction via `acompletion`)
- Telegram Bot API for messaging (via python-telegram-bot), faster-whisper for audio transcription
- ReportLab for PDF generation, Dropbox/Google Drive for file storage
- File-based storage (JSON, JSONL, Markdown): no database required
- uv + hatchling build system, ruff linting, ty type checking

## Storage

All data is stored as files under `data/users/` (configurable via `DATA_DIR`). No database is required.

```
data/
  user_index.json                    # Channel -> user_id routing
  seen_messages.json                 # Webhook idempotency (capped at 10K)
  users/
    {id}/
      user.json                      # Profile data
      SOUL.md                        # Personality/behavioral guidance
      USER.md                        # User info and preferences
      HEARTBEAT.md                   # Heartbeat items (single source of truth)
      memory/
        MEMORY.md                    # Structured facts by category
        HISTORY.md                   # Compaction log
      sessions/
        {session_id}.jsonl           # Conversation transcripts
      clients.json                   # Client records
      estimates/
        {estimate_id}.json           # Estimates with line items
      media.json                     # Media file manifest
      heartbeat/
        log.jsonl                    # Heartbeat send log
      llm_usage.jsonl                # Token usage log
```

Key store classes in `backend/app/agent/file_store.py`:
- `UserStore` (singleton via `get_user_store()`)
- `FileMemoryStore` (per-user via `get_memory_store(id)`)
- `FileSessionStore` (per-user via `get_session_store(id)`)
- `ClientStore`, `EstimateStore`, `MediaStore`, `HeartbeatStore` (instantiated per use)
- `IdempotencyStore` (singleton via `get_idempotency_store()`)
- `LLMUsageStore` (instantiated per use)

Data classes (Pydantic BaseModel, replace ORM models):
- `UserData`, `StoredMessage`, `SessionState`, `ClientData`
- `EstimateData`, `MediaData`, `HeartbeatItem`, `HeartbeatLogEntry`, `MemoryFact`

## Backwards Compatibility

Until this project has its first production release, you do not need to be concerned about backwards compatible changes.

## Coding Standards

- All type annotations required
- Ruff rules: `E, F, I, UP, B, SIM, ANN, RUF` (line length 100, `E501` and `B008` ignored)
- Pydantic v2 for all data classes and request/response schemas
- All routes `async def`
- All LLM calls via any-llm `acompletion` (async)
- Never use `BaseHTTPMiddleware` for streaming endpoints -- use pure ASGI middleware
- Conventional commit prefixes: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `ci:`, `chore:`
- Every data endpoint uses `Depends(get_current_user)` with `user_id` scoping
- Config via Pydantic `BaseSettings` with `extra="ignore"`
- Never use em dashes in user-facing content, comments, or copy -- use periods, commas, colons, or pipes instead

## Testing

- pytest with FastAPI `TestClient`
- File stores isolated per test via `tmp_path` + `settings.data_dir` patch
- `reset_stores()` clears cached store singletons between tests
- Override `get_current_user` via FastAPI dependency injection
- Mock ALL external services: Telegram, LLM (any-llm), faster-whisper, Dropbox/Drive
- Bug fixes must include regression tests

## Architecture

- **File-based storage**: all data in JSON/JSONL/Markdown files under `data/users/`. No database. See `backend/app/agent/file_store.py`.
- **Auth plugin infrastructure**: base.py (ABC), loader.py (dynamic import), dependencies.py (get_current_user), scoping.py (row-level auth). OSS is single-tenant; premium adds multi-tenant auth via plugin.
- **`user_id` scoping** on every data class and endpoint from day one
- **Message bus**: async inbound/outbound queues in `bus.py`. Channels publish inbound messages; the agent publishes outbound replies. The ``ChannelManager`` dispatches outbound messages to the correct channel.
- **Agent loop**: Telegram webhook -> media pipeline -> tool-calling loop (any-llm `acompletion`) -> tool execution -> reply
- **Memory**: MEMORY.md key-value facts + clients.json client records per user
- **Services**: External services abstracted behind service classes in `backend/app/services/`

## Definition of Done

Every change must pass all checks before it's considered complete:

```bash
uv run pytest -v                                  # tests pass
uv run ruff check backend/ tests/                 # lint passes
uv run ruff format --check backend/ tests/        # format passes
uv run ty check --python .venv backend/ tests/    # type checking passes
cd frontend && npm run deadcode                    # no dead JS/TS code (knip)
```

- Bug fixes include regression tests
- New features evaluate whether the docs site (`docs/`) needs updates
- When you manage a pull request, you must always adhere to the pull request template at .github/pull_request_template.md
- CI green

## Sandbox Tips

### Ephemeral directories

`target/`, `node_modules/`, and `.venv/` don't persist between sessions. Run `uv sync` at the start of each session if needed.

### Git operations

Git auth is pre-configured. Never push directly to main. Always create a branch and open a PR.

