#!/usr/bin/env bash
# Set up a public tunnel for Twilio webhooks during local development.
#
# Usage:
#   ./scripts/setup-tunnel.sh
#
# After running, copy the public URL and configure it in Twilio console:
#   Phone Numbers > Active Numbers > (your number) > Messaging > Webhook
#   Set to: https://<tunnel-url>/api/webhooks/twilio/inbound

set -euo pipefail

PORT="${PORT:-8000}"

# Try ngrok first, then fall back to localtunnel
if command -v ngrok &>/dev/null; then
    echo "Starting ngrok tunnel on port $PORT..."
    echo "Configure your Twilio webhook to: https://<ngrok-url>/api/webhooks/twilio/inbound"
    ngrok http "$PORT"
elif command -v npx &>/dev/null; then
    echo "Starting localtunnel on port $PORT..."
    echo "Configure your Twilio webhook to: https://<tunnel-url>/api/webhooks/twilio/inbound"
    npx localtunnel --port "$PORT"
else
    echo "Error: Neither ngrok nor npx (for localtunnel) found."
    echo ""
    echo "Install one of:"
    echo "  ngrok:       https://ngrok.com/download"
    echo "  localtunnel: npm install -g localtunnel"
    exit 1
fi
