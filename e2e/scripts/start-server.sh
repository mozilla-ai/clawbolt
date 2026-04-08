#!/usr/bin/env bash
# Start the OSS server for E2E tests.
# Usage: ./start-server.sh <port>
#
# Requires PostgreSQL to be running. Uses DATABASE_URL from the environment,
# or defaults to postgresql://clawbolt:clawbolt@localhost:5432/clawbolt_e2e.
#
# The script:
# 1. Builds the frontend if dist/ doesn't exist
# 2. Creates the e2e database if it doesn't exist
# 3. Runs Alembic migrations
# 4. Starts uvicorn with _verify_llm_settings monkey-patched out

set -euo pipefail

PORT="${1:?Usage: start-server.sh <port>}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

cd "$PROJECT_ROOT"

# Build frontend if dist/ doesn't exist
if [ ! -d "frontend/dist" ]; then
  echo "[e2e] Building frontend..."
  (cd frontend && npm install && npm run build)
fi

# Database setup
export DATABASE_URL="${DATABASE_URL:-postgresql://clawbolt:clawbolt@localhost:5432/clawbolt_e2e}"

echo "[e2e] DATABASE_URL=$DATABASE_URL"

# Create the e2e database if it doesn't exist.
# Try su (sandbox) first, fall back to PGPASSWORD + createdb (CI service containers).
DB_NAME="${DATABASE_URL##*/}"
if ! su - postgres -c "psql -tc \"SELECT 1 FROM pg_database WHERE datname='$DB_NAME'\" | grep -q 1 || psql -c \"CREATE DATABASE $DB_NAME OWNER clawbolt;\"" 2>/dev/null; then
  echo "[e2e] su - postgres failed, trying PGPASSWORD + createdb..."
  PGPASSWORD=clawbolt createdb -h localhost -U clawbolt "$DB_NAME" 2>/dev/null || true
fi

# Run Alembic migrations
echo "[e2e] Running Alembic migrations..."
uv run alembic upgrade head

# Start the server with _verify_llm_settings monkey-patched out
echo "[e2e] Starting server on port $PORT..."
exec uv run python -c "
import uvicorn

async def _noop():
    pass

from backend.app import main as _main
_main._verify_llm_settings = _noop
from backend.app.main import app
uvicorn.run(app, host='127.0.0.1', port=$PORT)
"
