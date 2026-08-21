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
            body='<html><body><div class="apply-wizard">Wizard content</div></body></html>',
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
