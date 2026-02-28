#!/usr/bin/env bash
# Set up a public tunnel for Telegram webhooks during local development.
#
# Usage:
#   ./scripts/setup-tunnel.sh
#
# After running, set the Telegram webhook using the tunnel URL:
#   curl -X POST "https://api.telegram.org/bot<TOKEN>/setWebhook" \
#     -H "Content-Type: application/json" \
#     -d '{"url": "https://<tunnel-url>/api/webhooks/telegram"}'

set -euo pipefail

PORT="${PORT:-8000}"

# Try ngrok first, then fall back to localtunnel
if command -v ngrok &>/dev/null; then
    echo "Starting ngrok tunnel on port $PORT..."
    echo "Set your Telegram webhook to: https://<ngrok-url>/api/webhooks/telegram"
    ngrok http "$PORT"
elif command -v npx &>/dev/null; then
    echo "Starting localtunnel on port $PORT..."
    echo "Set your Telegram webhook to: https://<tunnel-url>/api/webhooks/telegram"
    npx localtunnel --port "$PORT"
else
    echo "Error: Neither ngrok nor npx (for localtunnel) found."
    echo ""
    echo "Install one of:"
    echo "  ngrok:       https://ngrok.com/download"
    echo "  localtunnel: npm install -g localtunnel"
    exit 1
fi
