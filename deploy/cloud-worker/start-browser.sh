#!/usr/bin/env bash
# Phase 7.1: launches the persistent, real-Chrome browser service the
# worker attaches to over CDP. Run under systemd (dicepilot-browser.service)
# or manually for local testing -- never as part of a Flask/Vercel request.
#
# Uses a real Chrome (not Chromium/"Chrome for Testing") -- Google's own
# OAuth sign-in actively blocks the bundled Chrome-for-Testing build
# ("this browser or app may not be secure"), confirmed live during Phase 4B.1.
# Runs headed (under Xvfb) rather than --headless -- Dice's own login flow
# is more likely to resist a headless UA; a virtual display gives a real,
# inspectable Chrome window for the manual login step and for occasional
# operator re-authentication via the secure remote-access procedure in
# README.md, without ever exposing a display or debugging port publicly.
set -euo pipefail

: "${DICEPILOT_BROWSER_PROFILE_DIR:?Set DICEPILOT_BROWSER_PROFILE_DIR to a durable disk path, e.g. /opt/dicepilot/browser-profile}"
CDP_PORT="${DICEPILOT_CDP_PORT:-9333}"
DISPLAY_NUM="${DICEPILOT_DISPLAY:-:99}"

mkdir -p "$DICEPILOT_BROWSER_PROFILE_DIR"

# Xvfb: virtual framebuffer so Chrome can run headed without a physical
# display. Only reachable locally (no VNC/X server exposed on its own) --
# see README.md for the actual secure remote-viewing procedure.
Xvfb "$DISPLAY_NUM" -screen 0 1280x800x24 &
XVFB_PID=$!
trap 'kill "$XVFB_PID" 2>/dev/null || true' EXIT
export DISPLAY="$DISPLAY_NUM"

exec google-chrome \
  --user-data-dir="$DICEPILOT_BROWSER_PROFILE_DIR" \
  --remote-debugging-port="$CDP_PORT" \
  --remote-debugging-address=127.0.0.1 \
  --no-first-run \
  --no-default-browser-check \
  --disable-fre \
  --disable-features=Translate
