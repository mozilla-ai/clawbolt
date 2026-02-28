# Backshop

Backshop is an AI assistant for solo blue-collar contractors. FastAPI backend with a Telegram messaging interface and an any-agent TinyAgent agent loop. Built by Mozilla.ai using the open-core model.

## Tech Stack

- Python 3.11+, FastAPI, SQLAlchemy 2.0, Pydantic v2
- any-llm-sdk (LLM provider abstraction), any-agent (TinyAgent for agent loop)
- Telegram Bot API for messaging (via python-telegram-bot), faster-whisper for audio transcription
- ReportLab for PDF generation, Dropbox/Google Drive for file storage
- PostgreSQL (production), in-memory SQLite + StaticPool (tests)
- uv + hatchling build system, ruff linting

## Coding Standards

- All type annotations required
- Ruff rules: `E, F, I, UP, B, SIM, ANN, RUF` (line length 100, `E501` and `B008` ignored)
- SQLAlchemy 2.0 `mapped_column` style
- Pydantic v2 for all request/response schemas
- All routes `async def`
- All LLM calls via any-llm `acompletion` (async)
- Never use `BaseHTTPMiddleware` for streaming endpoints — use pure ASGI middleware
- Conventional commit prefixes: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `ci:`, `chore:`
- Every data endpoint uses `Depends(get_current_user)` with `user_id` scoping
- Config via Pydantic `BaseSettings` with `extra="ignore"`

## Testing

- pytest with FastAPI `TestClient`
- In-memory SQLite + `StaticPool` for all tests
- Override `get_db` and `get_current_user` via FastAPI dependency injection
- Mock ALL external services: Telegram, LLM (any-llm), faster-whisper, Dropbox/Drive
- Mock factories live in `tests/mocks/`
- Bug fixes must include regression tests

## Architecture

- **Auth plugin infrastructure**: base.py (ABC), loader.py (dynamic import), dependencies.py (get_current_user), scoping.py (row-level auth)
- **`user_id` scoping** on every model and endpoint from day one
- **MessagingService protocol**: channel-agnostic interface in `services/messaging.py` with Telegram implementation in `services/telegram_service.py`
- **Agent loop**: Telegram webhook -> media pipeline -> TinyAgent -> tool execution -> reply
- **Memory**: PostgreSQL key-value facts + client records
- **Services**: External services abstracted behind service classes in `backend/app/services/`

## Definition of Done

- Tests pass: `uv run pytest -v`
- Lint passes: `uv run ruff check backend/ tests/`
- Format passes: `uv run ruff format --check backend/ tests/`
- Bug fixes include regression tests
- CI green
