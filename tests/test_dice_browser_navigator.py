"""Phase 4B: navigator tests. Only opens already-discovered job URLs, never
runs Dice search, never clicks anything. Most cases use page.set_content()
against synthetic HTML — no live Dice needed."""
from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright

from dice_browser.models import BrowserState, ChallengeType
from dice_browser.navigator import InvalidJobUrlError, open_job, validate_canonical_url


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


# ── URL validation (no navigation needed) ────────────────────────────────


def test_validate_canonical_url_accepts_real_dice_job_detail_url():
    validate_canonical_url("https://www.dice.com/job-detail/469efdf8-e321-46a1-9346-70870d020736")


@pytest.mark.parametrize(
    "bad_url",
    [
        "https://evil.example.com/job-detail/abc",
        "https://www.dice.com/jobs?q=python",  # search page, not a job detail
        "https://www.dice.com/job-applications/abc/start-apply",  # apply-initiation, must never be opened here
        "not-a-url",
        "https://www.dice.com/",
    ],
)
def test_validate_canonical_url_rejects_non_job_detail_urls(bad_url):
    with pytest.raises(InvalidJobUrlError):
        validate_canonical_url(bad_url)


# ── open_job against synthetic pages (via page.goto to a data: URL is
# unreliable across browsers for complex markup, so we set_content after a
# real navigation stand-in — open_job takes an already-navigated Page). ──


def _prep(page, html: str, url: str = "https://www.dice.com/job-detail/fake-id-for-test"):
    # open_job() itself calls page.goto(); for offline tests we monkeypatch
    # goto to just set_content, since we're not hitting the network.
    # reload() is patched the same way -- open_job's own auth-recovery
    # retry calls page.reload(), which must re-render the same synthetic
    # content here, not fall through to a real navigation.
    def fake_goto(target_url, **kwargs):
        page.set_content(html)
        return None

    def fake_reload(**kwargs):
        page.set_content(html)
        return None

    page.goto = fake_goto
    page.reload = fake_reload
    return page


def _prep_sequence(page, html_sequence: list[str]):
    """Like _prep, but goto() renders html_sequence[0] and each reload()
    advances to the next entry -- simulates a page whose auth state
    changes between the initial load and a reload."""
    state = {"index": 0}

    def fake_goto(target_url, **kwargs):
        page.set_content(html_sequence[0])
        return None

    def fake_reload(**kwargs):
        state["index"] = min(state["index"] + 1, len(html_sequence) - 1)
        page.set_content(html_sequence[state["index"]])
        return None

    page.goto = fake_goto
    page.reload = fake_reload
    return page


def test_open_job_needs_input_on_challenge(page):
    _prep(page, "<html><body><p>Please complete the CAPTCHA to continue.</p></body></html>")
    result = open_job(page, "https://www.dice.com/job-detail/fake-id-for-test")
    assert result.browser_state == BrowserState.NEEDS_INPUT
    assert result.challenge_type == ChallengeType.CAPTCHA
    assert result.already_applied is None
    assert result.easy_apply_visible is None


def test_open_job_needs_input_when_auth_signals_ambiguous(page):
    # Neither a login form nor an account signal -- can't tell, must not
    # be silently treated as AUTH_REQUIRED.
    _prep(page, "<html><body><p>Some unrelated content, no auth signal at all.</p></body></html>")
    result = open_job(page, "https://www.dice.com/job-detail/fake-id-for-test")
    assert result.browser_state == BrowserState.NEEDS_INPUT
    assert result.authenticated is False
    assert result.already_applied is None


def test_open_job_auth_required_when_not_authenticated(page):
    _prep(
        page,
        """
        <html><body>
        <a href="/dashboard/login">Login</a>
        <apply-button-wc></apply-button-wc>
        </body></html>
        """,
    )
    result = open_job(page, "https://www.dice.com/job-detail/fake-id-for-test")
    assert result.browser_state == BrowserState.AUTH_REQUIRED
    assert result.authenticated is False
    # Easy Apply presence is a public signal (confirmed live in Phase 3B),
    # observable even logged out.
    assert result.easy_apply_visible is True
    # already_applied is inherently a per-account signal — unknown, not False, when logged out.
    assert result.already_applied is None


# Real root cause, live-found 2026-08-24/25: a fresh browser context's
# FIRST page load can show a logged-out header even with genuinely valid
# cookies -- a client-side hydration race, not a dead session. A single
# reload consistently resolves it live; these tests prove open_job()'s
# own reload-and-recheck retry, not the real Dice frontend.
_LOGIN_HTML = '<html><body><a href="/dashboard/login">Login</a></body></html>'
_AUTHENTICATED_HTML = '<html><body><nav aria-label="Account"></nav></body></html>'


def test_open_job_recovers_from_transient_auth_required_via_reload(page):
    _prep_sequence(page, [_LOGIN_HTML, _AUTHENTICATED_HTML])
    result = open_job(page, "https://www.dice.com/job-detail/fake-id-for-test")
    assert result.browser_state == BrowserState.ACTIVE
    assert result.authenticated is True
    assert "reload retry" in result.evidence


def test_open_job_stays_auth_required_when_reload_does_not_help(page):
    _prep_sequence(page, [_LOGIN_HTML, _LOGIN_HTML])
    result = open_job(page, "https://www.dice.com/job-detail/fake-id-for-test")
    assert result.browser_state == BrowserState.AUTH_REQUIRED
    assert result.authenticated is False


def test_open_job_never_reloads_when_already_authenticated_on_first_load(page):
    reload_calls = []
    _prep(page, _AUTHENTICATED_HTML)
    page.reload = lambda **kw: reload_calls.append(1)

    result = open_job(page, "https://www.dice.com/job-detail/fake-id-for-test")

    assert result.browser_state == BrowserState.ACTIVE
    assert reload_calls == []  # never reloads when the first load already resolved cleanly


def test_open_job_detects_easy_apply_absent(page):
    _prep(
        page,
        """
        <html><body>
        <a href="/dashboard/login">Login</a>
        <a href="/dashboard/login?redirectUrl=%2Fjob-applications%2Ffake%2Fstart-apply">Apply Now</a>
        </body></html>
        """,
    )
    result = open_job(page, "https://www.dice.com/job-detail/fake-id-for-test")
    assert result.easy_apply_visible is False


def test_open_job_detects_easy_apply_via_wizard_href_without_custom_element(page):
    # Real current Dice markup (confirmed live during Phase 4B validation)
    # no longer uses <apply-button-wc> at all — just a plain anchor. The
    # /wizard vs /start-apply href distinction (proven 20/20 in Phase 3B
    # live validation) is the actual reliable signal, read-only, never
    # navigated to or clicked.
    _prep(
        page,
        """
        <html><body>
        <a href="/dashboard/login">Login</a>
        <a href="/dashboard/login?redirectUrl=%2Fjob-applications%2Ffake%2Fwizard">Apply Now</a>
        </body></html>
        """,
    )
    result = open_job(page, "https://www.dice.com/job-detail/fake-id-for-test")
    assert result.easy_apply_visible is True


def test_open_job_detects_already_applied_when_authenticated(page):
    _prep(
        page,
        """
        <html><body>
        <nav><a href="/dashboard/logout">Sign Out</a></nav>
        <div class="ribbon-status-applied">Applied</div>
        <apply-button-wc></apply-button-wc>
        </body></html>
        """,
    )
    result = open_job(page, "https://www.dice.com/job-detail/fake-id-for-test")
    assert result.authenticated is True
    assert result.browser_state == BrowserState.ACTIVE
    assert result.already_applied is True


def test_open_job_rejects_invalid_url_before_navigating(page):
    with pytest.raises(InvalidJobUrlError):
        open_job(page, "https://www.dice.com/job-applications/abc/start-apply")


# Regression, live-found 2026-08-23 (Steel compatibility spike): a real
# job we know was successfully submitted showed already_applied=False --
# current Dice markup no longer emits .ribbon-status-applied at all. The
# real signal (also live-verified) is the same apply button rendered
# disabled with the exact accessible name "Applied".
def test_open_job_detects_already_applied_via_current_disabled_applied_button(page):
    _prep(
        page,
        """
        <html><body>
        <nav><a href="/dashboard/logout">Sign Out</a></nav>
        <button data-testid="apply-button" disabled>Applied</button>
        </body></html>
        """,
    )
    result = open_job(page, "https://www.dice.com/job-detail/fake-id-for-test")
    assert result.authenticated is True
    assert result.already_applied is True


# The old ribbon selector still works if Dice ever brings it back.
def test_open_job_still_detects_already_applied_via_legacy_ribbon(page):
    _prep(
        page,
        """
        <html><body>
        <nav><a href="/dashboard/logout">Sign Out</a></nav>
        <div class="ribbon-status-applied">Applied</div>
        </body></html>
        """,
    )
    result = open_job(page, "https://www.dice.com/job-detail/fake-id-for-test")
    assert result.already_applied is True


# A real false positive found live during this fix: generic "Create job
# alert" marketing copy ("...the job you applied for") appears on every
# job page regardless of application status and must never be mistaken
# for an actual applied-state indicator.
def test_open_job_does_not_false_positive_on_unrelated_applied_text(page):
    _prep(
        page,
        """
        <html><body>
        <nav><a href="/dashboard/logout">Sign Out</a></nav>
        <p>Never miss an opportunity! Create an alert based on the job you applied for.</p>
        <button data-testid="apply-button">Easy Apply</button>
        </body></html>
        """,
    )
    result = open_job(page, "https://www.dice.com/job-detail/fake-id-for-test")
    assert result.authenticated is True
    assert result.already_applied is False


def test_open_job_does_not_false_positive_on_unrelated_applied_mathematics_text(page):
    _prep(
        page,
        """
        <html><body>
        <nav><a href="/dashboard/logout">Sign Out</a></nav>
        <p>Candidates should have a degree in applied mathematics or a related field.</p>
        </body></html>
        """,
    )
    result = open_job(page, "https://www.dice.com/job-detail/fake-id-for-test")
    assert result.authenticated is True
    assert result.already_applied is False


def test_open_job_never_clicks_anything(page):
    # Regression guard: open_job must be pure inspection. Fail the test if
    # any click method is ever invoked on the page during a call.
    _prep(
        page,
        """
        <html><body>
        <a href="/dashboard/login">Login</a>
        <apply-button-wc></apply-button-wc>
        </body></html>
        """,
    )
    original_click = page.click
    calls = []
    page.click = lambda *a, **kw: calls.append((a, kw))
    try:
        open_job(page, "https://www.dice.com/job-detail/fake-id-for-test")
    finally:
        page.click = original_click
    assert calls == []
