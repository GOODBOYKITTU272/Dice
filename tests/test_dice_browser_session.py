"""Phase 4B: profile lock and auth/challenge detection tests.

Auth/challenge detection is tested against synthetic HTML via
page.set_content() — no live Dice needed for these. A real Chromium is
still used (via Playwright) since detection reads actual DOM state
through real locators, not string matching on raw HTML.
"""
from __future__ import annotations

import os

import pytest
from playwright.sync_api import sync_playwright

from dice_browser.models import BrowserState
from dice_browser.session import (
    ProfileInUseError,
    ProfileLock,
    classify_authentication,
    detect_challenge,
    is_authenticated,
)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture()
def page(browser):
    pg = browser.new_page()
    yield pg
    pg.close()


# ── profile lock ──────────────────────────────────────────────────────────


def test_profile_lock_acquire_and_release(tmp_path):
    lock = ProfileLock(tmp_path)
    lock.acquire()
    assert (tmp_path / ".dicepilot.lock").exists()
    lock.release()
    assert not (tmp_path / ".dicepilot.lock").exists()


def test_profile_lock_rejects_second_owner_while_first_pid_alive(tmp_path):
    lock1 = ProfileLock(tmp_path)
    lock1.acquire()  # writes our own (real, alive) pid

    lock2 = ProfileLock(tmp_path)
    with pytest.raises(ProfileInUseError):
        lock2.acquire()

    lock1.release()


def test_profile_lock_allows_reacquire_after_stale_pid(tmp_path):
    lock_path = tmp_path / ".dicepilot.lock"
    tmp_path.mkdir(parents=True, exist_ok=True)
    # A pid essentially guaranteed not to be alive right now.
    lock_path.write_text("999999")

    lock = ProfileLock(tmp_path)
    lock.acquire()  # must not raise — stale lock should be reclaimed
    assert lock_path.read_text().strip() == str(os.getpid())
    lock.release()


def test_profile_lock_release_only_removes_own_lock(tmp_path):
    lock_path = tmp_path / ".dicepilot.lock"
    tmp_path.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("999998")  # not our pid, not written by us

    lock = ProfileLock(tmp_path)
    lock.release()  # must be a no-op — we never acquired it
    assert lock_path.exists()


# ── auth detection ───────────────────────────────────────────────────────


def test_is_authenticated_false_when_login_form_present(page):
    page.set_content(
        """
        <html><body>
        <a href="/dashboard/login">Login</a>
        <input name="email" />
        <button>Sign in</button>
        </body></html>
        """
    )
    assert is_authenticated(page) is False


def test_is_authenticated_false_when_no_positive_signal_present(page):
    # Ambiguous page: no login form, but also no confirmed account signal.
    # Per "never guess", this must stay False/AUTH_REQUIRED, not True.
    page.set_content("<html><body><p>Some job posting content.</p></body></html>")
    assert is_authenticated(page) is False


def test_is_authenticated_true_when_account_signal_present(page):
    page.set_content(
        """
        <html><body>
        <nav><a href="/dashboard/logout">Sign Out</a></nav>
        </body></html>
        """
    )
    assert is_authenticated(page) is True


# ── challenge detection ──────────────────────────────────────────────────


def test_detect_challenge_none_on_plain_page(page):
    page.set_content("<html><body><h1>Senior Java Developer</h1></body></html>")
    assert detect_challenge(page) is None


def test_detect_challenge_otp(page):
    page.set_content("<html><body><p>Enter the verification code sent to your phone.</p></body></html>")
    from dice_browser.models import ChallengeType

    assert detect_challenge(page) == ChallengeType.OTP


def test_detect_challenge_captcha_by_text(page):
    page.set_content("<html><body><p>Please complete the CAPTCHA to continue.</p></body></html>")
    from dice_browser.models import ChallengeType

    assert detect_challenge(page) == ChallengeType.CAPTCHA


def test_detect_challenge_captcha_by_recaptcha_element(page):
    page.set_content('<html><body><div class="g-recaptcha"></div></body></html>')
    from dice_browser.models import ChallengeType

    assert detect_challenge(page) == ChallengeType.CAPTCHA


def test_detect_challenge_none_on_passive_recaptcha_disclosure_text(page):
    # Real live finding (Phase 4B.1 live closure, 2026-08-21): a genuine
    # authenticated Dice job page triggered a CAPTCHA false positive.
    # Root cause: "reCAPTCHA", lowercased, is "recaptcha" -- which
    # contains "captcha" as a substring. Google's invisible reCAPTCHA v3
    # badge injects a passive disclosure sentence like this one on a huge
    # fraction of the modern web, with no active challenge ever shown.
    page.set_content(
        "<html><body><p>This site is protected by reCAPTCHA and the Google "
        "Privacy Policy and Terms of Service apply.</p></body></html>"
    )
    assert detect_challenge(page) is None


def test_detect_challenge_none_on_invisible_recaptcha_badge_iframe(page):
    # The invisible v3 badge iframe is present in the DOM on huge numbers
    # of ordinary pages without ever presenting a challenge -- its mere
    # presence must not be treated as a security challenge.
    page.set_content(
        '<html><body><iframe src="https://www.google.com/recaptcha/api2/anchor" '
        'style="width:0;height:0;border:none;visibility:hidden"></iframe></body></html>'
    )
    assert detect_challenge(page) is None


def test_detect_challenge_security_check(page):
    page.set_content("<html><body><p>We noticed unusual activity — please verify it's you.</p></body></html>")
    from dice_browser.models import ChallengeType

    assert detect_challenge(page) == ChallengeType.SECURITY_CHECK


# ── Phase 4B.1: tri-state authentication classification ─────────────────
# is_authenticated() stays a simple bool for existing callers.
# classify_authentication() is richer: it distinguishes "confirmed logged
# out" from "can't tell" (neither/both signals present), since the two
# must never be conflated -- "never guess authenticated = True" applies
# just as much to silently defaulting an ambiguous page to AUTH_REQUIRED
# as it would to defaulting it to ACTIVE.


def test_classify_authentication_active_on_positive_fixture(page):
    page.set_content('<html><body><nav><a href="/dashboard/logout">Sign Out</a></nav></body></html>')
    assert classify_authentication(page) == BrowserState.ACTIVE


def test_classify_authentication_active_on_real_dice_account_nav_landmark(page):
    # Real live finding (Phase 4B.1 live closure, 2026-08-21): the
    # /dashboard/profiles "My Profile" link only appears in the home-feed
    # page's nav variant -- the job-detail page's simpler header doesn't
    # have it, and would incorrectly fall through to NEEDS_INPUT without
    # this. The nav[aria-label="Account"] landmark (wrapping the account
    # avatar button) is present consistently across both page types and
    # carries no personal data.
    page.set_content(
        '<html><body><nav aria-label="Account"><button aria-label="ramakrishna chanda">'
        '<span>RC</span></button></nav></body></html>'
    )
    assert classify_authentication(page) == BrowserState.ACTIVE


def test_classify_authentication_active_on_real_dice_my_profile_link(page):
    # Real live Dice authenticated-nav shape, confirmed via CDP attach to
    # a genuinely logged-in session (Phase 4B.1 live closure, 2026-08-21):
    # dashboard/logout / "Sign Out" text never actually appears anywhere
    # on the real page -- that guess was stale, exactly like
    # apply-button-wc in Phase 4B. The real positive signal is a
    # "My Profile" link to /dashboard/profiles.
    page.set_content('<html><body><nav><a href="https://www.dice.com/dashboard/profiles">My Profile</a></nav></body></html>')
    assert classify_authentication(page) == BrowserState.ACTIVE


def test_classify_authentication_auth_required_on_logged_out_fixture(page):
    page.set_content(
        """
        <html><body>
        <a href="/dashboard/login">Login</a>
        <input name="email" />
        </body></html>
        """
    )
    assert classify_authentication(page) == BrowserState.AUTH_REQUIRED


def test_classify_authentication_needs_input_when_neither_signal_present(page):
    # Ambiguous: no login form, no account signal either. Must not be
    # silently treated as AUTH_REQUIRED (that's a real logged-out-page
    # claim we haven't earned) or ACTIVE (never guessed).
    page.set_content("<html><body><p>Some job posting content.</p></body></html>")
    assert classify_authentication(page) == BrowserState.NEEDS_INPUT


def test_classify_authentication_needs_input_when_signals_conflict(page):
    # Both a login form AND an account-logout link present -- genuinely
    # conflicting evidence, must escalate rather than pick a side.
    page.set_content(
        """
        <html><body>
        <a href="/dashboard/login">Login</a>
        <a href="/dashboard/logout">Sign Out</a>
        </body></html>
        """
    )
    assert classify_authentication(page) == BrowserState.NEEDS_INPUT
