# Development

## Local Setup

```bash
pip install uv
uv sync
cp .env.example .env
# Edit .env with your credentials
uv run alembic upgrade head
uv run uvicorn backend.app.main:app --reload
```

You'll need a PostgreSQL instance running locally, or set `DATABASE_URL` accordingly.

## Running Tests

```bash
uv sync --all-extras
DATABASE_URL=sqlite:// uv run pytest -v
uv run ruff check backend/ tests/
uv run ruff format --check backend/ tests/
uv run ty check --python .venv backend/ tests/
```

Tests use in-memory SQLite, so no database setup is needed.

## More

For detailed guides on storage setup, Telegram webhooks, testing infrastructure, and troubleshooting, see the [full development docs](https://mozilla-ai.github.io/clawbolt/development/local-setup/).
