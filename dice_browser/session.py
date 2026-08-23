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
import socket
from pathlib import Path

from playwright.sync_api import BrowserContext, Page, sync_playwright

from dice_browser.models import BrowserState, ChallengeType

DEFAULT_PROFILE_ROOT = Path(__file__).resolve().parent.parent / ".runtime" / "browser_profiles"
BROWSER_PROFILE_DIR_ENV_VAR = "DICEPILOT_BROWSER_PROFILE_DIR"


def profile_root() -> Path:
    """DICEPILOT_BROWSER_PROFILE_DIR overrides the default local
    (.runtime/browser_profiles) location -- a cloud deployment points
    this at a durable disk path (e.g. /opt/dicepilot/browser-profile) so
    the persistent Chromium profile survives worker restarts and VM
    reboots, never at ephemeral /tmp. Read at call time, not import time,
    so tests can override the env var per-test."""
    override = os.environ.get(BROWSER_PROFILE_DIR_ENV_VAR)
    return Path(override) if override else DEFAULT_PROFILE_ROOT


def profile_dir_for(profile_id: str, root: Path | None = None) -> Path:
    return (root if root is not None else profile_root()) / profile_id


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
# Action-oriented phrasing only -- NOT a bare "captcha" substring match.
# Real live finding (Phase 4B.1 live closure, 2026-08-21): "reCAPTCHA",
# lowercased, is "recaptcha", which contains "captcha" -- so a bare
# substring check fires on Google's ubiquitous passive invisible-badge
# disclosure sentence ("This site is protected by reCAPTCHA...") that
# appears on huge numbers of ordinary pages with no challenge ever shown.
_CAPTCHA_PHRASES = ("complete the captcha", "solve the captcha", "captcha verification", "captcha challenge")


def detect_challenge(page: Page) -> ChallengeType | None:
    text = (page.inner_text("body") if page.locator("body").count() > 0 else "").lower()

    # .g-recaptcha is the classic visible checkbox-widget container -- a
    # real interactive challenge, not a passive badge.
    if page.locator(".g-recaptcha").count() > 0:
        return ChallengeType.CAPTCHA
    # A captcha/recaptcha-sourced iframe only counts if actually VISIBLE --
    # Google's invisible v3 badge iframe is deliberately excluded here
    # (its mere presence in the DOM is not itself a challenge; see the
    # regression test). Checked defensively (element could detach mid-check).
    for frame_el in page.locator("iframe[src*='captcha' i], iframe[title*='captcha' i]").all():
        try:
            if frame_el.is_visible():
                return ChallengeType.CAPTCHA
        except Exception:
            continue
    if any(phrase in text for phrase in _CAPTCHA_PHRASES):
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
    # Verified live (Phase 4B.1 CDP-attach closure, 2026-08-21) against a
    # genuinely authenticated Dice session, across two different page
    # types (home-feed and job-detail, which render different header
    # variants): nav[aria-label="Account"] (wrapping the account avatar
    # button) is present consistently on both -- unlike the
    # /dashboard/profiles "My Profile" link, which only appears in the
    # home-feed nav variant and would leave job-detail pages incorrectly
    # falling through to NEEDS_INPUT. dashboard/logout / "Sign Out" never
    # appeared anywhere on the real page -- that original guess was
    # stale, exactly like apply-button-wc in Phase 4B. All kept as
    # fallback signals in case a different Dice UI variant shows them.
    if page.locator("nav[aria-label='Account']").count() > 0:
        return True
    if page.locator("a[href*='dashboard/profiles']").count() > 0:
        return True
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


_SINGLETON_LOCK_ARTIFACTS = ("SingletonLock", "SingletonCookie", "SingletonSocket")


def _singleton_lock_is_stale(lock_target: str) -> bool:
    """Chrome writes SingletonLock as a symlink to '<hostname>-<pid>' of
    the process holding it. Stale means: a hostname that isn't this host
    (a different, now-dead container instance -- exactly what a Docker
    container recreation leaves behind), or a pid that isn't alive on
    this host. Anything we can't parse is treated as NOT stale -- when
    in doubt, never touch a lock that might be protecting a real,
    running Chrome."""
    hostname, sep, pid_str = lock_target.rpartition("-")
    if not sep:
        return False  # malformed -- can't confirm staleness, so don't touch it
    if hostname != socket.gethostname():
        return True  # well-formed lock for a different host/container instance -- definitely stale
    try:
        pid = int(pid_str)
    except ValueError:
        return False
    return not _pid_is_alive(pid)


def clean_stale_singleton_locks(profile_dir: str | Path) -> list[str]:
    """Removes ONLY Chrome's own SingletonLock/SingletonCookie/
    SingletonSocket artifacts from a persistent profile directory, and
    only when SingletonLock is confirmed stale (see
    _singleton_lock_is_stale). Never touches Cookies, Login Data, Local
    Storage, IndexedDB, History, Preferences, Session Storage, or
    anything else in the profile -- this function only ever looks at
    (and removes) the three names above, nothing else.

    Exists for exactly the failure this project hit live: a Steel/Docker
    container recreation can leave Chrome's own stale lock behind,
    refusing the next launch ("profile appears to be in use by another
    Chromium process") even though the profile itself (and its saved
    Dice login) is perfectly intact. Call this once before starting
    Chrome against a durable, shared profile directory -- never while a
    real Chrome process might still be using it (the staleness check is
    the actual safety gate, not just calling this at the "right time")."""
    profile_dir = Path(profile_dir)
    lock_path = profile_dir / "SingletonLock"
    if not lock_path.is_symlink():
        return []

    target = os.readlink(lock_path)
    if not _singleton_lock_is_stale(target):
        return []

    removed = []
    for name in _SINGLETON_LOCK_ARTIFACTS:
        artifact = profile_dir / name
        if artifact.is_symlink() or artifact.exists():
            artifact.unlink()
            removed.append(name)
    return removed
