#!/usr/bin/env bash
# Phase 7.1: quick operator/monitoring check -- CDP reachable + worker
# heartbeat fresh. Exits non-zero if either looks wrong. Safe to run from
# cron/a monitoring agent; read-only, no Dice navigation.
set -euo pipefail

CDP_URL="${DICEPILOT_CDP_URL:-http://127.0.0.1:9333}"

echo "Checking CDP endpoint at $CDP_URL ..."
if ! curl -fsS -m 5 "$CDP_URL/json/version" > /dev/null; then
  echo "FAIL: CDP endpoint not reachable at $CDP_URL"
  exit 1
fi
echo "OK: CDP reachable."

echo "Checking worker heartbeat via Supabase ..."
cd "$(dirname "$0")/../.."
.venv/bin/python -c "
import sys
import run_registry
status = run_registry.worker_status()
print(status)
sys.exit(0 if status['online'] else 1)
"
