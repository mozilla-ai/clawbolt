# Clawbolt

Clawbolt is an AI assistant for solo blue-collar contractors. FastAPI backend with a Telegram messaging interface and a custom tool-calling agent loop built on any-llm. Built by Mozilla.ai using the open-core model.

## Build & Run Commands

```bash
# Install dependencies
uv sync

# Run server
uv run uvicorn backend.app.main:app --reload

# Tests
DATABASE_URL=sqlite:// uv run pytest -v

# Lint & format
uv run ruff check backend/ tests/
uv run ruff format --check backend/ tests/

# Type checking
uv run ty check --python .venv backend/ tests/

# Database migrations
uv run alembic upgrade head
uv run alembic revision --autogenerate -m "description"
```

## Tech Stack

- Python 3.11+, FastAPI, SQLAlchemy 2.0, Pydantic v2
- any-llm-sdk (LLM provider abstraction via `acompletion`)
- Telegram Bot API for messaging (via python-telegram-bot), faster-whisper for audio transcription
- ReportLab for PDF generation, Dropbox/Google Drive for file storage
- PostgreSQL (production), in-memory SQLite + StaticPool (tests)
- uv + hatchling build system, ruff linting, ty type checking

## Coding Standards

- All type annotations required
- Ruff rules: `E, F, I, UP, B, SIM, ANN, RUF` (line length 100, `E501` and `B008` ignored)
- SQLAlchemy 2.0 `mapped_column` style
- Pydantic v2 for all request/response schemas
- All routes `async def`
- All LLM calls via any-llm `acompletion` (async)
- Never use `BaseHTTPMiddleware` for streaming endpoints -- use pure ASGI middleware
- Conventional commit prefixes: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `ci:`, `chore:`
- Every data endpoint uses `Depends(get_current_user)` with `user_id` scoping
- Config via Pydantic `BaseSettings` with `extra="ignore"`
- Never use em dashes in user-facing content, comments, or copy -- use periods, commas, colons, or pipes instead

## Testing

- pytest with FastAPI `TestClient`
- In-memory SQLite + `StaticPool` for all tests
- Override `get_db` and `get_current_user` via FastAPI dependency injection
- Mock ALL external services: Telegram, LLM (any-llm), faster-whisper, Dropbox/Drive
- Mock factories live in `tests/mocks/`
- Bug fixes must include regression tests

## Architecture

- **Auth plugin infrastructure**: base.py (ABC), loader.py (dynamic import), dependencies.py (get_current_user), scoping.py (row-level auth). OSS is single-tenant; premium adds multi-tenant auth via plugin.
- **`user_id` scoping** on every model and endpoint from day one
- **MessagingService protocol**: channel-agnostic interface in `services/messaging.py` with Telegram implementation in `services/telegram_service.py`
- **Agent loop**: Telegram webhook -> media pipeline -> tool-calling loop (any-llm `acompletion`) -> tool execution -> reply
- **Memory**: PostgreSQL key-value facts + client records
- **Services**: External services abstracted behind service classes in `backend/app/services/`

## Definition of Done

Every change must pass all checks before it's considered complete:

```bash
uv run pytest -v                                  # tests pass
uv run ruff check backend/ tests/                 # lint passes
uv run ruff format --check backend/ tests/        # format passes
uv run ty check --python .venv backend/ tests/    # type checking passes
```

- Bug fixes include regression tests
- CI green

## Sandbox Tips

### Ephemeral directories

`target/`, `node_modules/`, and `.venv/` don't persist between sessions. Run `uv sync` at the start of each session if needed.

### Git operations

Git auth is pre-configured. Never push directly to main. Always create a branch and open a PR.

### Fixing broken git worktrees

Git worktrees store absolute paths. When a worktree is created on the host and the sandbox mounts the same tree at a different path, the cross-references break. Fix by rewriting the paths:

```bash
HOST_PREFIX="/Users/you/scm/clawbolt"   # adjust to match your host
SANDBOX_PREFIX="/workspace/clawbolt"

# Fix main repo -> worktree references
sed -i "s|$HOST_PREFIX|$SANDBOX_PREFIX|g" .git/worktrees/*/gitdir 2>/dev/null

# Fix worktree -> main repo back-references
find .claude/worktrees -maxdepth 2 -name ".git" -type f \
  -exec sed -i "s|$HOST_PREFIX|$SANDBOX_PREFIX|g" {} \; 2>/dev/null

# Verify
git worktree list
```

### Multiple repos in workspace

The workspace root `/workspace/clawbolt` is the OSS repo. Premium (`clawbolt-premium/`) and infra (`clawbolt-infra/`) are separate git repos cloned as subdirectories, listed in `.gitignore`. Do not commit files from those repos into the OSS repo.
