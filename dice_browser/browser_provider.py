"""Phase 7.2/7.6: which browser DicePilot's worker connects to.

Deliberately a thin config layer, not a class hierarchy: local Chrome
(Mac dev, CDP-attach to an already-running, already-authenticated
browser DicePilot never owns), self-hosted Steel Browser (Docker, same
CDP-attach call -- confirmed unmodified during the Steel compatibility
spike, 2026-08-23), and Browserless (Phase 7.6 -- hosted remote Chrome,
live-verified to persist a cookie-authenticated Dice session across
reconnects) all connect the exact same way, via
dice_browser.worker_daemon's existing playwright.chromium.connect_over_cdp(cdp_url).
For Browserless, DICEPILOT_CDP_URL is simply the session's own persisted
`connect` websocket URL (Browserless's "Persisting State" REST API --
see attention-service-era session bootstrap script, not committed) --
no extra per-connect setup needed, unlike Steel. The only real
per-provider difference is whether it's safe/useful to run
dice_browser.session.clean_stale_singleton_locks() before connecting --
Steel manages a durable, shared Chrome profile that a container
recreation can leave locked; local mode's Chrome is a long-lived,
manually-started process DicePilot never restarts, and Browserless's
persisted session is server-side, so neither has anything to clean up.
"""
from __future__ import annotations

import os

BROWSER_PROVIDER_ENV_VAR = "DICEPILOT_BROWSER_PROVIDER"
VALID_PROVIDERS = ("local", "steel", "browserless")
DEFAULT_PROVIDER = "local"


def resolve_browser_provider() -> str:
    """Never raises on a bad value -- an unrecognized provider safely
    falls back to "local" (the one that's always worked) rather than
    failing the whole daemon startup over a typo'd env var."""
    value = os.environ.get(BROWSER_PROVIDER_ENV_VAR, DEFAULT_PROVIDER).strip().lower()
    return value if value in VALID_PROVIDERS else DEFAULT_PROVIDER
