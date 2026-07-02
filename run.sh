#!/usr/bin/env bash
# Launch both services: the AI gateway (owns the mock upstream) on :8090 and the
# chat app on :8000. Ctrl-C stops both.
set -euo pipefail
cd "$(dirname "$0")"

if [ -d .venv ]; then source .venv/bin/activate; fi

echo "Starting ai-gateway on :8090 (mock Anthropic upstream on :8080)…"
uvicorn gateway.main:app --port 8090 &
GW=$!

# Give the gateway + mock a moment to bind.
sleep 3

echo "Starting chat service on :8000…"
uvicorn app.main:app --port 8000 &
CH=$!

trap 'echo; echo "Stopping…"; kill $GW $CH 2>/dev/null || true' INT TERM
echo "Open http://localhost:8000  (gateway health: http://localhost:8090/healthz)"
wait
