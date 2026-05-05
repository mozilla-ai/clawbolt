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
- any-llm-sdk (LLM provider abstraction via `amessages`)
- Telegram Bot API for messaging (via python-telegram-bot)
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

## Database access (sync + async dual-API)

OSS is mid-migration from sync DB sessions to async (epic #1139). Every store currently exposes both a sync and an async surface so internal callers can convert one site at a time and external consumers (premium, CLI, Alembic) keep working unchanged. The pattern below is the contract that landed in PRs #1189, #1198, #1199, #1200, #1201, #1202, #1203, #1204, #1205, #1206. New stores and new methods MUST follow it; the sync surface will be removed in #1160 only after premium migration completes.

### Dual-API contract

- Sync methods keep their plain names (`load`, `get_by_id`, `create`, ...).
- Async peers add an `_async` suffix (`load_async`, `get_by_id_async`, ...).
- Sync method bodies must not change observable behavior when an async peer is added. Refactor only as far as extracting shared pure builders. Do not change return types, exception classes, query shapes, ordering, or locking.
- Do not preemptively delete sync APIs. Removal lands in #1160 once premium converts.
- Do not accept an externally-managed session as a parameter on the `_async` methods. Mismatch with the sync API contract; not how callers use the methods.

### Shared logic via pure builders

Sync and async methods share query construction through module-level builders that return concretely-typed SQLAlchemy core constructs. No `Any`, no session dependency. Canonical pilot: `_seen_select`, `_count_select`, `_prune_delete` in `backend/app/agent/stores.py:578-600` (IdempotencyStore). Same shape across `HeartbeatStore`, `MediaStore`, `LLMUsageStore`, `ToolConfigStore`, `MemoryStore`, `UserStore`, `SessionStore`, `DbSettingsStore`.

Non-SQL helpers that both paths need (allowlist setters like `_apply_media_updates`, time-window helpers like `_today_window_utc`) follow the same rule: pure, module-level, no session.

```python
def _seen_select(external_id: str) -> Select[tuple[IdempotencyKey]]:
    return select(IdempotencyKey).filter_by(external_id=external_id)
```

### Async session lifecycle

Both factories live in `backend/app/database.py`. Async pool tuning matches the sync pool (`pool_recycle`, `pool_pre_ping`, statement timeout), and the async session factory ships with `expire_on_commit=False` so attribute access after commit does not trigger `MissingGreenlet`.

- READ-only async methods: `db = AsyncSessionLocal()` + `try / finally await db.close()`. Lighter weight, no rollback wrapper. Reference `IdempotencyStore.has_seen_async` in `backend/app/agent/stores.py:628-635`.
- WRITE async methods: `async with db_session_async() as db: ...`. Auto-rollback on exception, auto-close. Reference `IdempotencyStore.try_mark_seen_async` in `backend/app/agent/stores.py:661-683`.

### Common SQLAlchemy 2.0 patterns

These are the patterns that survive the sync-to-async port. PR #1190 already converted all `db.query()` call sites; do not reintroduce the 1.x Query API in new code.

- Read: `db.execute(select(X).where(...)).scalar_one_or_none()` works on both `Session` and `AsyncSession` (with `await` on the async side). For a scalar count use `db.scalar(select(func.count(...)))`.
- DML rowcount: at runtime `db.execute(update/delete).rowcount` returns `int`, but the stubs say `Result`. Cast to access cleanly: `cast("CursorResult[object]", db.execute(...)).rowcount`. Reference `SessionStore.delete_message` in `backend/app/agent/session_db.py:794-823`.
- Bulk DML `synchronize_session`: the kwarg moved off `update()`/`delete()` constructors. Use `.execution_options(synchronize_session="fetch")` on the executable, and preserve the original value when migrating call sites. Reference `_append_history_update` in `backend/app/agent/memory_db.py:66-79`.
- Row-level lock: `db.execute(select(M).filter_by(id=x).with_for_update()).scalar_one_or_none()`.
- `.scalars().all()` returns `Sequence[T]`, not `list[T]`. Wrap with `list(...)` only when the consumer is typed for `list`.

### Encrypted columns: do not concat on the SQL side

`EncryptedString` columns (e.g. `MemoryDocument.history_text`, `OAuthToken.access_token`) handle envelope encryption automatically on bind/unbind. SQL-side string concat (`Model.col || new_text`) operates on ciphertext and silently corrupts the row. Bug fixed in #1200.

Correct pattern: SELECT FOR UPDATE the row, decrypt-in-Python via attribute access, append in Python, UPDATE with the full new plaintext. Reference `_doc_select_for_update` and `_append_history_update` plus their callers in `backend/app/agent/memory_db.py:52-237`. The row-level lock serializes concurrent appenders so neither side loses its update.

### Advisory locks

Use `pg_advisory_xact_lock` whenever possible: the lock is bound to the surrounding transaction and released automatically on COMMIT or ROLLBACK. Both sync and async paths simply execute the lock SQL inside their session and let the existing commit drop it. Reference `_advisory_lock_sql` in `backend/app/agent/session_db.py:100-111` (SessionStore) and `_lock_user_permissions` in `backend/app/agent/approval.py`.

Session-scoped advisory locks (`pg_advisory_lock` / `pg_try_advisory_lock`) are different: the unlock MUST run on the **same** connection that took the lock. SQLAlchemy `Session.commit()` returns the underlying DBAPI connection to the pool, so a follow-up `pg_advisory_unlock` call on a fresh `SessionLocal()` runs on a different connection and is a silent no-op (Postgres returns `False`, the helper logs nothing). Recovery code in `backend/app/agent/inbound_recovery.py` and OAuth refresh in `backend/app/services/oauth.py` rely on the same-connection coupling. The regression test in `tests/test_inbound_recovery.py:715` (`test_unlock_on_different_connection_is_a_no_op`) pins this invariant for the future async port.

Concurrency tests for advisory locks: spin per-thread `engine.connect()` (sync) or per-task `AsyncSession(engine, ...)` connections so the lock primitive is actually exercised, not the connection serialization. Coordinate via `threading.Event` (sync) or `asyncio.Event` (async). Do not assert on `time.monotonic()` deltas across threads or tasks; sub-millisecond races make the comparisons flake (lesson from #1202). Reference `tests/test_approval.py:216-385` (`TestApprovalLockSerialization`) and `tests/test_inbound_recovery.py:457-790` (`TestInboundRecoveryLockSerialization`).

### Test fixture: async DB isolation

The `async_db` fixture in `tests/conftest.py:171-211` runs each async test inside a per-test `AsyncConnection` with a wrapping transaction; it rebinds `backend.app.database._async_session_factory` so store calls to `AsyncSessionLocal()` and `db_session_async()` pick up the test connection. Two non-obvious choices documented in the design comment block above the fixture (lines 119-168):

- **Function-scoped engine.** asyncpg connections bind to the event loop they were created on; pytest-asyncio rotates loops between tests by default. A session-scoped async engine surfaces as `RuntimeError: Future attached to a different loop` on the second test. We pay one engine setup per async test in exchange for not having to widen the loop scope across the whole suite.
- **`join_transaction_mode="create_savepoint"`, not `conditional_savepoint`.** The sync analog uses `conditional_savepoint` because psycopg2 keeps the outer transaction alive when only the savepoint aborts on `IntegrityError`. Under asyncpg the outer transaction silently detaches in the same scenario, which surfaces as "the row I just committed disappeared after a duplicate-insert error in a later session". `create_savepoint` forces every session into its own SAVEPOINT and keeps the contract consistent across drivers.

The shared `async_test_user` fixture (`tests/conftest.py:248-280`) inserts a test user through the async connection, expunges it, and yields. Use it from any async store test; do not redefine.

End every async test file with an iso-canary pair to prove rollback isolation. The `_part_a` test writes a fixed-id row; the `_part_b` test asserts the row is gone. Reference `test_async_isolation_rolls_back_between_tests_part_a` and `_part_b` in `tests/test_idempotency_pruning_async.py`.

Cross-API caveat: the sync per-test transaction (`_isolate_stores`) and the async per-test transaction (`async_db`) live on independent connections. Under READ COMMITTED, a sync write committed from one is not visible to an async read in the same test. Pure-async store tests are fine; mixed-API tests must drive their setup through the matching API.

### Premium

Premium imports OSS via the editable `../clawbolt` path. The dual-API surface lets premium migrate call sites incrementally without lockstep merges. Premium ships its own `async_db` fixture that mirrors the OSS one but rebinds the OSS module attributes (`_oss_db_module._async_engine`, `_async_session_factory`); see premium #390 when it lands. The sync removal in #1160 is gated on premium completing its store conversions.

## Backwards Compatibility

Until this project has its first production release, you do not need to be concerned about backwards compatible changes.

## Coding Standards

- All type annotations required
- Ruff rules: `E, F, I, UP, B, SIM, ANN, RUF` (line length 100, `E501` and `B008` ignored)
- SQLAlchemy 2.0 `mapped_column` style for all ORM models
- Pydantic v2 for all data classes and request/response schemas
- All routes `async def`
- All LLM calls via any-llm `amessages` (async)
- Never use `BaseHTTPMiddleware` for streaming endpoints -- use pure ASGI middleware
- Conventional commit prefixes: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `ci:`, `chore:`
- Every data endpoint uses `Depends(get_current_user)` with `user_id` scoping
- Config via Pydantic `BaseSettings` with `extra="ignore"`
- Prefer `isinstance` checks and direct typed attribute access over `getattr`, `hasattr`, or string-based type checks. Our objects are properly typed; using `getattr` with defaults masks real problems when types change. Only use `getattr`/`hasattr` when explicitly directed or at true dynamic boundaries (e.g. plugin APIs).
- Never use em dashes in user-facing content, comments, or copy -- use periods, commas, colons, or pipes instead
- All imports at the top of the file. No inline or deferred imports inside functions. The only exception is `TYPE_CHECKING` guarded imports.

## Privacy & PII

**Never write down real names or real personal information anywhere that gets persisted or shared.** This includes, but is not limited to:

- Source code (comments, docstrings, string literals, variable names)
- Tests and test fixtures (use obviously fake names like `Alice`, `Bob`, `Test User`, or domain-appropriate placeholders)
- Documentation (READMEs, AGENTS.md, CLAUDE.md, SKILL.md, user guides, design docs)
- Commit messages, branch names, and PR titles/descriptions/comments
- GitHub issues, discussions, and any other public artifact
- Migration files, seed data, and example payloads
- Logs, error messages, and debug output that may be checked in

Real PII to avoid: real customer/user names, real phone numbers, real email addresses, real addresses, real business names from customer data, real Telegram handles or chat IDs, real OAuth tokens or API keys.

**Soft PII**: use judgment for content that isn't on the hard list above but still ties back to a specific user. Generalize in your own writing.

When you need realistic-looking data, use clearly synthetic values: `jane.doe@example.com`, `+15555550123`, `Acme Plumbing`, UUIDs, or `faker`-style placeholders. If you encounter real PII in a debugging session or pasted content, scrub it before committing or pushing anything to GitHub.

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
- **Agent loop**: Telegram webhook -> media pipeline -> tool-calling loop (any-llm `amessages`) -> tool execution -> reply
- **Memory**: Freeform per-user MEMORY.md managed via workspace tools, backed by `memory_documents` table with automatic compaction
- **Services**: External services abstracted behind service classes in `backend/app/services/`

## Adding a New Agent Tool

The agent's capabilities are extended by adding tools. Tools follow a factory/registry pattern with auto-discovery.

### Core vs. Specialist

- **Core tools** (`core=True`): Always available to the agent on every message. Use for universal capabilities (math, messaging, files, workspace). No activation step needed.
- **Specialist tools** (`core=False`): Activated on demand via the `list_capabilities` meta-tool. Use for integrations and domain-specific features (calendar, QuickBooks, CompanyCam). Keeps the initial tool schema small.

### Checklist for adding a tool

1. **Create the tool module** at `backend/app/agent/tools/<name>_tools.py`. Follow the pattern in `heartbeat_tools.py` (simplest example): Pydantic params model, async tool function returning `ToolResult`, factory function, and `_register()` called at module level. The `_tools` suffix is required for auto-discovery.

2. **Add tool name constants** to `backend/app/agent/tools/names.py` in the `ToolName` class. All tool name strings must be defined here to prevent silent breakage on renames.

3. **Register in the dashboard** at `backend/app/routers/user_tools.py`:
   - Add the factory name to `_CORE_FACTORIES` (if core) so it cannot be disabled
   - Add a `_FACTORY_META` entry with a description (and `domain_group`/`domain_group_order` if specialist)

4. **Add to the registry test** at `tests/test_tool_registry.py`: add `"backend.app.agent.tools.<name>_tools"` to `EXPECTED_TOOL_MODULES`.

5. **Wire up approval policies** for any mutating tool. If a `SubToolInfo` declares `default_permission="ask"`, the corresponding `Tool` object **must** have `approval_policy=ApprovalPolicy(default_level=PermissionLevel.ASK)`. Without this, the WebUI shows "ask" but the runtime auto-executes. See `quickbooks_tools.py` for the reference pattern. The global test `test_ask_sub_tools_have_approval_policy` in `test_tool_registry.py` enforces this.

6. **Set a `concurrency_group` if your tool mutates shared state.** The agent runs all approved tool calls from a single LLM turn concurrently by default. Tools with the same non-None `concurrency_group` serialize in submission order; tools with different keys (or `None`) may run in parallel. Set this whenever your tool could race with another tool in the same turn against a shared resource, for example a DB row, a workspace document, a disk file, or the user-facing message stream. Read-only and stateless tools should leave it `None`. Accepts either a static string or a callable that takes the validated args and returns a key, for the case where a single tool routes to distinct resources by argument (e.g. workspace writers keyed by file path). Existing keys: `"workspace_path:<path>"` for workspace document mutations (resolved per call by `_workspace_path_concurrency_key`), `"user_outbound"` for reply senders, `"user_integrations"` for integration toggles. The global test `test_state_mutating_tools_have_concurrency_group` in `test_tool_registry.py` enforces that any tool tagged `MODIFIES_PROFILE` or `SENDS_REPLY` declares one.

7. **Write tests** at `tests/test_<name>_tools.py`. Call the factory function directly (e.g., `_create_calculator_tools()`) and invoke the tool function. No database needed for stateless tools.

8. **(Specialist only) Add a SKILL.md** at `backend/app/agent/skills/<name>/SKILL.md` if the tool has complex workflows the LLM needs guidance on. This markdown is injected into the conversation when the LLM activates the category via `list_capabilities`. Core tools do not need SKILL.md; their `description` and `usage_hint` fields in the Python code serve the same purpose.

### Key files

| File | Purpose |
|---|---|
| `backend/app/agent/tools/base.py` | `Tool`, `ToolResult`, `ToolErrorKind` definitions |
| `backend/app/agent/tools/names.py` | All tool name constants (`ToolName` class) |
| `backend/app/agent/tools/registry.py` | `ToolRegistry`, `ToolFactory`, `ToolContext`, auto-discovery |
| `backend/app/agent/skills/loader.py` | SKILL.md loader (`get_skill_instructions`) |
| `backend/app/routers/user_tools.py` | Dashboard wiring (`_CORE_FACTORIES`, `_FACTORY_META`) |

## Editing prompt files (SKILL.md, system prompts)

`SKILL.md` files and the agent system prompt are injected into the LLM context on every relevant turn, so prose costs tokens on every conversation. Be terse and non-redundant when editing them.

- **Do not duplicate information that already lives elsewhere in the file.** If a field's shape is shown in a payload example or an example workflow, do not re-document it in a separate "field shapes" preamble. The agent reads the whole file.
- **State rules at the failure mode, not as top-of-section framing.** A "do not claim X is unavailable" warning belongs inside the workflow that prevents X. A general preamble at the top of a section is usually padding the headings or steps already imply.
- **Trust the steps.** A numbered workflow does not need an intro paragraph explaining when to use it; the heading and the cross-references from other sections already do that.
- **Cut padding.** Phrases like "Use this whenever ...", "If a field is listed here, the entity has it ...", "It is important to ..." are framing the structure already implies. Delete the sentence; if the meaning is intact, it was redundant.

After editing, read the diff and ask: did I add a new fact, or restate an old one? Restated facts double the prompt without doubling agent behavior.

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

When backend schemas change (`backend/app/schemas.py`, route signatures, response models, or endpoint docstrings), you **must** regenerate the frontend OpenAPI types. Never hand-edit `frontend/src/generated/api.d.ts`. CI will fail if the committed file doesn't match what the generator produces.

```bash
uv run python scripts/export_openapi.py           # export openapi.json from backend
cd frontend && npm run generate:api                # regenerate src/generated/api.d.ts
```

Commit both `frontend/openapi.json` and `frontend/src/generated/api.d.ts`.

- Bug fixes include regression tests
- New features evaluate whether the user docs (`frontend/src/docs-content/`) need updates
- Features that change how users interact with the assistant must update the user guide (`frontend/src/docs-content/guide/`)
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
