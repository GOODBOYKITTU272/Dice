"""Read-only, non-mutating checks against the persistent CDP-attached
Dice browser (see STATE.md's CDP-attach architecture). Never launches a
browser, never navigates, never clicks -- only inspects what's already
open. No cookies or tokens are ever read or returned from here.
"""
from __future__ import annotations

import os
from typing import Any

import requests

CDP_URL = os.environ.get("DICEPILOT_CDP_URL", "http://127.0.0.1:9333")
_HTTP_TIMEOUT_SECONDS = 1.5

# In-memory only -- this is a single-process local dev server; no need for
# a DB table just to remember the last on-demand recheck result.
_last_full_check: dict[str, Any] | None = None


def check_connection() -> dict[str, Any]:
    """Fast, HTTP-only reachability check against the CDP endpoint --
    safe to run on every page load."""
    try:
        resp = requests.get(f"{CDP_URL}/json/list", timeout=_HTTP_TIMEOUT_SECONDS)
        resp.raise_for_status()
        tabs = resp.json()
    except Exception as exc:  # noqa: BLE001 - surfaced to the operator
        return {"connected": False, "error": str(exc), "dice_tab_url": None, "tab_count": 0}

    dice_tabs = [t.get("url", "") for t in tabs if t.get("url", "").startswith(("https://www.dice.com", "https://dice.com"))]
    return {
        "connected": True,
        "error": None,
        "tab_count": len(tabs),
        "dice_tab_url": dice_tabs[0] if dice_tabs else None,
    }


def last_full_check() -> dict[str, Any] | None:
    return _last_full_check


def run_full_check() -> dict[str, Any]:
    """On-demand only (the "Recheck Session" button) -- opens a short-lived
    Playwright connection to read auth state from whichever Dice tab is
    already open, then disconnects. Never navigates anywhere new."""
    global _last_full_check

    from playwright.sync_api import sync_playwright

    from dice_browser.session import classify_authentication, detect_challenge

    result: dict[str, Any] = {"connected": False, "authenticated": None, "current_url": None, "challenge": None, "error": None}
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(CDP_URL)
            result["connected"] = True
            ctx = browser.contexts[0]
            dice_pages = [pg for pg in ctx.pages if "dice.com" in pg.url]
            if dice_pages:
                page = dice_pages[0]
                result["current_url"] = page.url
                result["authenticated"] = classify_authentication(page).value
                challenge = detect_challenge(page)
                result["challenge"] = challenge.value if challenge else None
            browser.close()
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)

    _last_full_check = result
    return result
