# Development

## Local Setup

```bash
pip install uv
uv sync
cp .env.example .env
# Edit .env with your credentials
uv run uvicorn backend.app.main:app --reload
```

No database is required. All data is stored as files (JSON, JSONL, Markdown) under
`data/users/` by default. Set the `DATA_DIR` environment variable to change the
storage location.

## Running Tests

```bash
uv sync --all-extras
uv run pytest -v
uv run ruff check backend/ tests/
uv run ruff format --check backend/ tests/
uv run ty check --python .venv backend/ tests/
```

Tests use temporary directories for file-based storage, so no external services
are needed.

## More

For detailed guides on storage setup, Telegram webhooks, testing infrastructure, and troubleshooting, see the [full development docs](https://mozilla-ai.github.io/clawbolt/development/local-setup/).
