# Backshop

AI assistant for solo blue-collar contractors. Built by Mozilla.ai.

## Quick Start

```bash
pip install uv
uv sync
uv run uvicorn backend.app.main:app --reload
```

## Development

```bash
uv sync --all-extras
uv run pytest -v
uv run ruff check backend/ tests/
uv run ruff format --check backend/ tests/
```
