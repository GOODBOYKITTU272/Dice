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
    original_goto = page.goto

    def fake_goto(target_url, **kwargs):
        page.set_content(html)
        return None

    page.goto = fake_goto
    return page


def test_open_job_needs_input_on_challenge(page):
    _prep(page, "<html><body><p>Please complete the CAPTCHA to continue.</p></body></html>")
    result = open_job(page, "https://www.dice.com/job-detail/fake-id-for-test")
    assert result.browser_state == BrowserState.NEEDS_INPUT
    assert result.challenge_type == ChallengeType.CAPTCHA
    assert result.already_applied is None
    assert result.easy_apply_visible is None


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
