"""Phase 4B.1: manual authenticated-session bootstrap.

Run as: python -m dice_browser.session_bootstrap [profile_id]

Launches the existing persistent Chromium profile in VISIBLE (headed)
mode, opens Dice's login page, and then waits -- untouched, no DOM
polling of the live page at all -- for an explicit local signal file to
appear. Only once that signal exists does this script look at the page
even once, to check authentication. Never types credentials, never
solves a challenge (OTP/CAPTCHA/security check); the human handles all
of that directly in the window.

The signal file exists so a human (or the process orchestrating this
script) can say "I'm done" without this script needing to touch Dice's
page repeatedly in the meantime -- it's a local file check only.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from dice_browser.session import (
    ProfileInUseError,
    classify_authentication,
    detect_challenge,
    launch_persistent_session,
)

DICE_LOGIN_URL = "https://www.dice.com/dashboard/login"
SIGNAL_FILE = Path("/tmp/dicepilot_login_ready_signal")
SIGNAL_POLL_SECONDS = 2


def main() -> int:
    profile_id = sys.argv[1] if len(sys.argv) > 1 else "primary-candidate"

    if SIGNAL_FILE.exists():
        SIGNAL_FILE.unlink()

    print(f"Acquiring persistent profile '{profile_id}' (real Chrome channel)...", flush=True)
    try:
        # channel="chrome": real installed Google Chrome, not the bundled
        # Chrome-for-Testing build -- Google's own OAuth sign-in blocks
        # the latter as "not secure" (confirmed live, Phase 4B.1).
        lock, context = launch_persistent_session(profile_id, headless=False, channel="chrome")
    except ProfileInUseError as exc:
        print(f"BLOCKED: {exc}", flush=True)
        return 1

    page = context.new_page()
    page.goto(DICE_LOGIN_URL, wait_until="domcontentloaded")

    print("READY", flush=True)
    print("Browser window is open. Log in manually (including any OTP/CAPTCHA/security step).", flush=True)
    print(f"Waiting for signal file at {SIGNAL_FILE} — not touching the page until then.", flush=True)

    while not SIGNAL_FILE.exists():
        time.sleep(SIGNAL_POLL_SECONDS)

    SIGNAL_FILE.unlink()
    print("Signal received — inspecting the current page once now.", flush=True)
    print("current_url:", page.url, flush=True)
    print("page_title:", page.title(), flush=True)

    challenge = detect_challenge(page)
    if challenge is not None:
        print(f"NEEDS_INPUT: security challenge detected ({challenge.value})", flush=True)
    else:
        state = classify_authentication(page)
        print("classify_authentication:", state, flush=True)

    print(
        "\nLeaving browser open. This process holds the profile lock — "
        "terminate it to release the lock and close the browser.",
        flush=True,
    )
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
