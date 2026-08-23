#!/usr/bin/env bash
# Phase 7.1: quick operator/monitoring check -- CDP reachable + worker
# heartbeat fresh. Exits non-zero if either looks wrong. Safe to run from
# cron/a monitoring agent; read-only, no Dice navigation.
set -euo pipefail

PROVIDER="${DICEPILOT_BROWSER_PROVIDER:-local}"

if [ "$PROVIDER" = "steel" ]; then
  # Steel's CDP URL is ws://..., which curl can't check directly -- its
  # HTTP API lives on the same host:port, so probe that instead.
  CDP_URL="${DICEPILOT_CDP_URL:-ws://localhost:3000/}"
  HEALTH_URL="$(echo "$CDP_URL" | sed 's#^ws#http#')v1/sessions"
else
  CDP_URL="${DICEPILOT_CDP_URL:-http://127.0.0.1:9333}"
  HEALTH_URL="$CDP_URL/json/version"
fi

echo "Checking browser provider ($PROVIDER) at $HEALTH_URL ..."
if ! curl -fsS -m 5 "$HEALTH_URL" > /dev/null; then
  echo "FAIL: browser endpoint not reachable at $HEALTH_URL"
  exit 1
fi
echo "OK: browser endpoint reachable."

echo "Checking worker heartbeat via Supabase ..."
cd "$(dirname "$0")/../.."
.venv/bin/python -c "
import sys
import run_registry
status = run_registry.worker_status()
print(status)
sys.exit(0 if status['online'] else 1)
"
