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

# Try cloudflared first, then fall back to localtunnel
if command -v cloudflared &>/dev/null; then
    echo "Starting Cloudflare Tunnel on port $PORT..."
    echo "Copy the https://*.trycloudflare.com URL from the output below."
    cloudflared tunnel --url "http://localhost:$PORT"
elif command -v npx &>/dev/null; then
    echo "Starting localtunnel on port $PORT..."
    echo "Set your Telegram webhook to: https://<tunnel-url>/api/webhooks/telegram"
    npx localtunnel --port "$PORT"
else
    echo "Error: Neither cloudflared nor npx (for localtunnel) found."
    echo ""
    echo "Install one of:"
    echo "  cloudflared: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
    echo "  localtunnel: npm install -g localtunnel"
    exit 1
fi
