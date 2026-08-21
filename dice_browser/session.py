"""Phase 4B: persistent authenticated Playwright session for one Dice
candidate profile.

Foundation only. This module launches/reuses a persistent browser context
and detects auth/challenge state — it never fills the login form itself
(no real Dice credentials have a source anywhere in this project yet; see
STATE.md) and never solves a challenge. A challenge always becomes
NEEDS_INPUT, never a bypass attempt.
"""
from __future__ import annotations

import os
from pathlib import Path

from playwright.sync_api import BrowserContext, Page, sync_playwright

from dice_browser.models import BrowserState, ChallengeType

DEFAULT_PROFILE_ROOT = Path(__file__).resolve().parent.parent / ".runtime" / "browser_profiles"


def profile_dir_for(profile_id: str, root: Path = DEFAULT_PROFILE_ROOT) -> Path:
    return root / profile_id


class ProfileInUseError(RuntimeError):
    pass


class ProfileLock:
    """Single-owner guard for one persistent Chromium user-data directory.
    A local pidfile lock — sufficient for one-candidate, one-machine V1;
    deliberately not a distributed lock (YAGNI — see Phase 4B loop scope
    in STATE.md)."""

    def __init__(self, profile_dir: Path):
        self._path = Path(profile_dir) / ".dicepilot.lock"

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._path.exists():
            existing_pid = _read_pid(self._path)
            if existing_pid is not None and _pid_is_alive(existing_pid):
                raise ProfileInUseError(
                    f"Browser profile at {self._path.parent} is already in use by pid {existing_pid}"
                )
            # Stale lock (process no longer running, or unreadable) — reclaim it.
        self._path.write_text(str(os.getpid()))

    def release(self) -> None:
        if not self._path.exists():
            return
        owner_pid = _read_pid(self._path)
        if owner_pid == os.getpid():
            self._path.unlink()

    def __enter__(self) -> "ProfileLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (ValueError, OSError):
        return None


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # process exists, just owned by someone else
    return True


def launch_persistent_session(
    profile_id: str, headless: bool = False, channel: str | None = None
) -> tuple[ProfileLock, BrowserContext]:
    """Launch (or reuse) one persistent Chromium profile. Caller owns the
    returned lock and context and must release/close both when done —
    this function does not manage a context manager scope itself so the
    caller can drive multiple page navigations in between.

    channel: Playwright's standard browser-channel option (e.g. "chrome"
    to use a real installed Google Chrome instead of the bundled
    Chromium/"Chrome for Testing" build). This is a documented Playwright
    feature, not a fingerprint/stealth trick — it doesn't hide the
    automation flag or spoof anything; it just runs a different, real
    browser binary. Used because Google's own OAuth sign-in actively
    blocks the bundled Chrome-for-Testing build ("this browser or app may
    not be secure") — confirmed live during Phase 4B.1."""
    profile_dir = profile_dir_for(profile_id)
    lock = ProfileLock(profile_dir)
    lock.acquire()
    playwright = sync_playwright().start()
    context = playwright.chromium.launch_persistent_context(
        str(profile_dir),
        headless=headless,
        channel=channel,
    )
    context._dicepilot_playwright = playwright  # keep alive for close(); see close_persistent_session
    return lock, context


def close_persistent_session(lock: ProfileLock, context: BrowserContext) -> None:
    playwright = getattr(context, "_dicepilot_playwright", None)
    context.close()
    if playwright is not None:
        playwright.stop()
    lock.release()


# ── auth / challenge detection ───────────────────────────────────────────
# Conservative by design (see NavigationResult.already_applied docstring
# and STATE.md "never guess"): these only return a positive claim when a
# specific signal is present. is_authenticated()'s POSITIVE signal (an
# account/logout element) has not yet been verified against a real
# authenticated Dice session — no Dice credentials exist anywhere in this
# project (see STATE.md Phase 4B). The NEGATIVE signals (login form,
# /dashboard/login, a "Login" link) *have* been confirmed against live
# Dice pages (Phase 3B browser validation). Until a real authenticated
# session is available to verify the positive path, treat a True result
# here with appropriate caution — it is not yet live-proven, only the
# False path is.

_OTP_PHRASES = ("verification code", "one-time code", "enter the code", "one-time password", " otp ")
_SECURITY_PHRASES = ("verify it's you", "security check", "device verification", "unusual activity")


def detect_challenge(page: Page) -> ChallengeType | None:
    text = (page.inner_text("body") if page.locator("body").count() > 0 else "").lower()

    if page.locator(".g-recaptcha, iframe[src*='captcha'], iframe[title*='captcha' i]").count() > 0:
        return ChallengeType.CAPTCHA
    if "captcha" in text:
        return ChallengeType.CAPTCHA
    if any(phrase in text for phrase in _OTP_PHRASES):
        return ChallengeType.OTP
    if any(phrase in text for phrase in _SECURITY_PHRASES):
        return ChallengeType.SECURITY_CHECK
    return None


def _has_negative_auth_signal(page: Page) -> bool:
    if page.locator("input[name='email']").count() > 0:
        return True
    if "/dashboard/login" in page.url:
        return True
    if page.locator("a[href*='dashboard/login']").count() > 0:
        return True
    if page.get_by_text("Login", exact=True).count() > 0:
        return True
    return False


def _has_positive_auth_signal(page: Page) -> bool:
    # See module-level caveat above: not yet live-verified against a real
    # authenticated session (Phase 4B.1 is what verifies/corrects this).
    if page.locator("a[href*='dashboard/logout']").count() > 0:
        return True
    if page.get_by_text("Sign Out", exact=False).count() > 0:
        return True
    return False


def is_authenticated(page: Page) -> bool:
    """Simple bool for callers that just want yes/no. True only on a
    confirmed positive signal with no conflicting negative one. Use
    classify_authentication() when the ambiguous/conflicting case needs
    its own handling instead of collapsing into False."""
    return classify_authentication(page) == BrowserState.ACTIVE


def classify_authentication(page: Page) -> BrowserState:
    """Tri-state: ACTIVE (positive signal, no conflict), AUTH_REQUIRED
    (negative signal, no conflict), or NEEDS_INPUT — used both when
    signals conflict (both present) and when neither is present (can't
    tell). Those two "ambiguous" cases are deliberately never conflated
    with a confirmed AUTH_REQUIRED or a guessed ACTIVE — "never guess
    authenticated = True" extends to never silently assuming logged-out
    just because nothing else was found."""
    negative = _has_negative_auth_signal(page)
    positive = _has_positive_auth_signal(page)
    if negative and positive:
        return BrowserState.NEEDS_INPUT
    if positive:
        return BrowserState.ACTIVE
    if negative:
        return BrowserState.AUTH_REQUIRED
    return BrowserState.NEEDS_INPUT
