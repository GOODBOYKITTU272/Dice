"""Phase 4C: Easy Apply precondition gate and opening. This is the ONLY
module in the codebase permitted to navigate into /job-applications/... --
gated behind three preconditions that must ALL hold before any click.
Offline tests only (synthetic HTML via page.set_content()); no live Dice
needed for any of this.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright

from dice_browser.easy_apply import open_easy_apply
from dice_browser.models import BrowserState, NavigationResult


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


def _nav_result(**overrides) -> NavigationResult:
    defaults = dict(
        canonical_url="https://www.dice.com/job-detail/fake-id",
        page_title="Fake Job",
        browser_state=BrowserState.ACTIVE,
        authenticated=True,
        already_applied=False,
        easy_apply_visible=True,
        challenge_type=None,
        evidence="test fixture",
    )
    defaults.update(overrides)
    return NavigationResult(**defaults)


def _track_clicks(page):
    calls = []
    original_click = page.click
    page.click = lambda *a, **kw: calls.append((a, kw)) or original_click(*a, **kw)
    return calls


# ── precondition refusals (zero click in every case) ─────────────────────


def test_refuses_when_not_authenticated(page):
    page.set_content("<html><body><a href='/job-applications/x/wizard'>Apply Now</a></body></html>")
    result = open_easy_apply(page, _nav_result(authenticated=False))
    assert result.opened is False
    assert result.reason == "AUTH_REQUIRED"


def test_refuses_when_already_applied_true(page):
    page.set_content("<html><body><a href='/job-applications/x/wizard'>Apply Now</a></body></html>")
    result = open_easy_apply(page, _nav_result(already_applied=True))
    assert result.opened is False
    assert result.reason == "ALREADY_APPLIED"


def test_refuses_when_already_applied_ambiguous(page):
    page.set_content("<html><body><a href='/job-applications/x/wizard'>Apply Now</a></body></html>")
    result = open_easy_apply(page, _nav_result(already_applied=None))
    assert result.opened is False
    assert result.reason == "UNKNOWN_APPLIED_STATE"  # never assumed False


def test_refuses_when_not_easy_apply(page):
    page.set_content("<html><body><a href='/job-applications/x/start-apply'>Apply Now</a></body></html>")
    result = open_easy_apply(page, _nav_result(easy_apply_visible=False))
    assert result.opened is False
    assert result.reason == "NOT_EASY_APPLY"


def test_no_click_occurs_on_any_precondition_refusal(page):
    page.set_content("<html><body><a href='/job-applications/x/wizard'>Apply Now</a></body></html>")
    calls = _track_clicks(page)
    open_easy_apply(page, _nav_result(authenticated=False))
    open_easy_apply(page, _nav_result(already_applied=True))
    open_easy_apply(page, _nav_result(already_applied=None))
    open_easy_apply(page, _nav_result(easy_apply_visible=False))
    assert calls == []


# ── successful open, given all preconditions hold ────────────────────────


def test_opens_when_all_preconditions_pass_and_wizard_evidence_present(page):
    # page.route() intercepts the real navigation the click triggers --
    # more realistic than a JS pushState hack, and avoids about:blank's
    # unreliable-origin quirks for anchor-click navigation.
    page.route(
        "**/job-applications/abc123/wizard",
        lambda route: route.fulfill(
            status=200,
            content_type="text/html",
            body="<html><head><title>Apply | Dice.com</title></head><body>"
            "<h1>You're Applying for</h1></body></html>",
        ),
    )
    page.set_content(
        '<html><body><a href="https://www.dice.com/job-applications/abc123/wizard">Apply Now</a></body></html>'
    )
    result = open_easy_apply(page, _nav_result())
    assert result.opened is True
    assert result.reason == "OPENED"


def test_opens_correctly_when_wizard_evidence_arrives_slightly_after_click(page):
    # Real live finding (Phase 4B.1 CDP-attach closure, 2026-08-21): on a
    # genuinely authenticated live click, the wizard DID open (confirmed
    # by inspecting the page moments later) but open_easy_apply() reported
    # CLICK_FAILED -- Dice's apply flow appears to be a client-side SPA
    # route change, not always a full page reload, so
    # wait_for_load_state("domcontentloaded") isn't a reliable signal that
    # the new content has actually rendered yet. This fixture simulates a
    # click handler that updates the DOM slightly after the click
    # returns (via setTimeout), and the fix must tolerate that with a
    # short bounded wait rather than checking exactly once immediately.
    # pushState/replaceState to a cross-origin URL silently no-ops from
    # about:blank, so a real https://www.dice.com/... origin is
    # established first via route interception before simulating the
    # delayed client-side transition -- this is what lets replaceState
    # actually take effect in this offline test, same-origin.
    page.route(
        "**/job-detail/abc123",
        lambda route: route.fulfill(
            status=200,
            content_type="text/html",
            body="""
            <html><body>
            <a id="apply-link" href="/job-applications/abc123/wizard">Apply Now</a>
            <script>
            document.getElementById('apply-link').addEventListener('click', (e) => {
                e.preventDefault();
                setTimeout(() => {
                    history.replaceState({}, '', '/job-applications/abc123/wizard');
                    document.title = 'Apply | Dice.com';
                    document.body.innerHTML = "<h1>You're Applying for</h1>";
                }, 600);
            });
            </script>
            </body></html>
            """,
        ),
    )
    page.goto("https://www.dice.com/job-detail/abc123")
    result = open_easy_apply(page, _nav_result())
    assert result.opened is True
    assert result.reason == "OPENED"


def test_opens_on_real_dice_wizard_page_shape(page):
    # Real live finding (Phase 4B.1 CDP-attach closure, 2026-08-21): the
    # guessed [class*='apply-wizard']/[data-testid*='apply-wizard'] DOM
    # landmark never appears anywhere on the real wizard page -- confirmed
    # by direct inspection of a genuinely opened Dice apply flow. The real
    # page has no wizard-specific data-testid at all; the reliable
    # landmarks are the exact page title ("Apply | Dice.com") and the
    # visible "You're Applying for" heading text.
    page.route(
        "**/job-applications/abc123/wizard",
        lambda route: route.fulfill(
            status=200,
            content_type="text/html",
            body="<html><head><title>Apply | Dice.com</title></head><body>"
            "<h1>You're Applying for</h1><p>Data Engineer @ Stefanini</p>"
            "<p>Step 1 of 2</p><p>Resume &amp; Cover Letter</p>"
            "</body></html>",
        ),
    )
    page.set_content(
        '<html><body><a href="https://www.dice.com/job-applications/abc123/wizard">Apply Now</a></body></html>'
    )
    result = open_easy_apply(page, _nav_result())
    assert result.opened is True
    assert result.reason == "OPENED"


def test_click_failed_when_click_occurs_but_no_wizard_evidence(page):
    # Click succeeds (real navigation happens) but the resulting page
    # shows no wizard signal at all -- must not be reported as opened.
    page.route(
        "**/job-applications/abc123/wizard",
        lambda route: route.fulfill(
            status=200,
            content_type="text/html",
            body="<html><body><p>Something unrelated happened.</p></body></html>",
        ),
    )
    page.set_content(
        '<html><body><a href="https://www.dice.com/job-applications/abc123/wizard">Apply Now</a></body></html>'
    )
    result = open_easy_apply(page, _nav_result())
    assert result.opened is False
    assert result.reason.startswith("CLICK_FAILED")


def test_stale_non_wizard_apply_link_is_rejected_even_if_nav_result_says_visible(page):
    # nav_result claims easy_apply_visible=True (e.g. stale from an
    # earlier check), but the LIVE page's apply link is actually a
    # start-apply (non-wizard) link -- must re-verify at click time, not
    # trust the possibly-stale flag.
    page.set_content("<html><body><a href='/job-applications/x/start-apply'>Apply Now</a></body></html>")
    calls = _track_clicks(page)
    result = open_easy_apply(page, _nav_result(easy_apply_visible=True))
    assert result.opened is False
    assert result.reason.startswith("CLICK_FAILED")
    assert calls == []


def test_no_duplicate_click_if_already_on_apply_flow(page):
    # Simulates calling open_easy_apply a second time after the first
    # call already navigated away from the job-detail page -- the apply
    # link locator won't be found, so it must refuse rather than attempt
    # another click.
    page.set_content('<html><body><div class="apply-wizard">Already in wizard</div></body></html>')
    calls = _track_clicks(page)
    result = open_easy_apply(page, _nav_result())
    assert result.opened is False
    assert result.reason.startswith("CLICK_FAILED")
    assert calls == []
