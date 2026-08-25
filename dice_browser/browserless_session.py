"""Phase 7.9: dynamic Browserless session provisioning for the Railway
worker.

Never depends on one static, manually-copied session URL -- that's
exactly what expired mid-development and needed a human to notice and
fix by hand. Every connection creates a brand-new Browserless session
via their REST API, then loads the candidate's persisted Dice login
(exported once via the manual Cookie-Editor procedure, stored as an env
var -- never a file on any one machine, so any worker instance can use
it) into that fresh session's cookies before any navigation.

Known, accepted V1 limitation: Browserless's free tier has no
interactive re-login flow (LiveURL is a paid-plan feature, confirmed
live). When Dice itself invalidates the stored cookies, a human must
manually re-export them (same Cookie-Editor procedure) and update
DICE_AUTH_COOKIES_JSON -- this module surfaces that as a clean
"not authenticated" signal (dice_browser.worker's existing AUTH_REQUIRED
StopReason), never a crash, never a bypass attempt.
"""
from __future__ import annotations

import json
import os

import requests

BROWSERLESS_TOKEN_ENV_VAR = "BROWSERLESS_TOKEN"
BROWSERLESS_REGION_ENV_VAR = "BROWSERLESS_REGION"
DEFAULT_BROWSERLESS_REGION = "production-sfo.browserless.io"
DICE_COOKIES_ENV_VAR = "DICE_AUTH_COOKIES_JSON"
DEFAULT_SESSION_TTL_MS = 21600000  # 6 hours -- generous for one work cycle; never held open idle regardless
DEFAULT_PROCESS_KEEP_ALIVE_MS = 60000
_REQUEST_TIMEOUT_SECONDS = 20

_SAME_SITE_MAP = {"lax": "Lax", "strict": "Strict", "no_restriction": "None", None: "Lax"}


class BrowserlessNotConfiguredError(RuntimeError):
    pass


def is_configured() -> bool:
    return bool(os.environ.get(BROWSERLESS_TOKEN_ENV_VAR))


def _token() -> str:
    token = os.environ.get(BROWSERLESS_TOKEN_ENV_VAR)
    if not token:
        raise BrowserlessNotConfiguredError(f"{BROWSERLESS_TOKEN_ENV_VAR} is not configured")
    return token


def _region() -> str:
    return os.environ.get(BROWSERLESS_REGION_ENV_VAR, DEFAULT_BROWSERLESS_REGION)


def create_session(ttl_ms: int = DEFAULT_SESSION_TTL_MS, process_keep_alive_ms: int = DEFAULT_PROCESS_KEEP_ALIVE_MS) -> dict:
    """Creates a brand-new Browserless session via their REST API --
    never reuses a previously-created session's connect URL. Returns the
    raw API response: {id, connect, stop, ttl, ...}."""
    resp = requests.post(
        f"https://{_region()}/session",
        params={"token": _token()},
        json={"ttl": ttl_ms, "processKeepAlive": process_keep_alive_ms},
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    return resp.json()


def stop_session(stop_url: str) -> None:
    """Best-effort -- never raises. A session that's already ended
    (TTL/keepAlive elapsed) returns 404, which is fine, not an error."""
    try:
        requests.post(stop_url, timeout=_REQUEST_TIMEOUT_SECONDS)
    except Exception:
        pass


def load_dice_cookies() -> list[dict] | None:
    """The single-global-account cookie set (pre-Phase-M8B). Kept only as
    the explicit, opt-in dev fallback inside load_dice_cookies_for_
    candidate() below -- no production call site should call this
    directly anymore. Returns None if not configured."""
    raw = os.environ.get(DICE_COOKIES_ENV_VAR)
    if not raw:
        return None
    return json.loads(raw)


_DEV_FALLBACK_ENV_VAR = "DICEPILOT_ALLOW_GLOBAL_DICE_COOKIES_FALLBACK"


def load_dice_cookies_for_candidate(candidate_id: str) -> list[dict] | None:
    """Phase M8B: the one production entrypoint for loading Dice auth --
    always candidate-scoped (db.dice_auth_state_repository, Vault-backed).
    Returns None (never another candidate's cookies, never a silent
    substitution) if this candidate has no ACTIVE auth state.

    The old global DICE_AUTH_COOKIES_JSON is reachable ONLY when
    DICEPILOT_ALLOW_GLOBAL_DICE_COOKIES_FALLBACK is explicitly set --
    unset in every real deployment, so a production multi-user run can
    never activate it by accident. When it does fire, this logs plainly
    that dev-fallback mode is active (never the cookie values
    themselves)."""
    import logging

    from db.dice_auth_state_repository import get_auth_state

    raw = get_auth_state(candidate_id)
    if raw:
        return json.loads(raw)

    if os.environ.get(_DEV_FALLBACK_ENV_VAR) == "true":
        logging.getLogger(__name__).warning(
            "DEV MODE: candidate %s has no candidate-scoped Dice auth state -- "
            "falling back to the global DICE_AUTH_COOKIES_JSON dev fallback "
            "(DICEPILOT_ALLOW_GLOBAL_DICE_COOKIES_FALLBACK=true). Never expect "
            "this in a production multi-user run.",
            candidate_id,
        )
        return load_dice_cookies()

    return None


def to_playwright_cookies(raw_cookies: list[dict]) -> list[dict]:
    """Converts the Cookie-Editor export format (what the manual re-auth
    procedure produces) into what Playwright's
    BrowserContext.add_cookies() expects."""
    converted = []
    for c in raw_cookies:
        converted.append(
            {
                "name": c["name"],
                "value": c["value"],
                "domain": c["domain"],
                "path": c["path"],
                "secure": c["secure"],
                "httpOnly": c["httpOnly"],
                "sameSite": _SAME_SITE_MAP.get(c.get("sameSite"), "Lax"),
                "expires": c["expirationDate"],
            }
        )
    return converted
