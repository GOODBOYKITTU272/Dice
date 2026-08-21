"""Phase 4C: opens the Dice Easy Apply flow for one pre-qualified,
already-authenticated job.

This is the ONLY module in this codebase permitted to navigate into
/job-applications/... -- gated behind three preconditions that must ALL
hold before any click occurs. Discovery code (dice/search.py,
dice/job_parser.py, dice/discovery.py, dice_browser/navigator.py) remains
permanently forbidden from this path; that boundary is not loosened here
or anywhere else.

Stops the instant the wizard is confirmed open. No question answering, no
Next/Review/Submit -- those modules don't exist anywhere in this repo.
"""
from __future__ import annotations

from playwright.sync_api import Page

from dice_browser.models import EasyApplyOpenResult, NavigationResult


def open_easy_apply(page: Page, nav_result: NavigationResult) -> EasyApplyOpenResult:
    if not nav_result.authenticated:
        return _refuse(page, "AUTH_REQUIRED")
    if nav_result.already_applied is None:
        return _refuse(page, "UNKNOWN_APPLIED_STATE")  # ambiguous -- never assumed False
    if nav_result.already_applied:
        return _refuse(page, "ALREADY_APPLIED")
    if not nav_result.easy_apply_visible:
        return _refuse(page, "NOT_EASY_APPLY")

    # Re-verify against the LIVE page at click time -- nav_result may be
    # stale (time passed, or the page state shifted since it was built).
    apply_link = page.locator("a[href*='job-applications']").first
    if apply_link.count() == 0:
        return _refuse(page, "CLICK_FAILED", "no job-applications apply link found on the live page")

    href = apply_link.get_attribute("href") or ""
    if "wizard" not in href:
        return _refuse(page, "CLICK_FAILED", f"live apply link is not a wizard entry: {href!r}")

    apply_link.click()
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15_000)
    except Exception:
        pass  # best-effort settle; the bounded poll below is what actually decides success

    # Real live finding (Phase 4B.1 CDP-attach closure, 2026-08-21): Dice's
    # apply flow behaves like a client-side SPA route change on at least
    # some paths, so wait_for_load_state("domcontentloaded") isn't a
    # reliable signal that the new content has rendered -- a single
    # immediate check reported CLICK_FAILED even though the wizard had, in
    # fact, opened moments later. Poll briefly (bounded, not aggressive)
    # rather than trust one snapshot in time.
    if not _poll_for_wizard_opened(page):
        return EasyApplyOpenResult(
            opened=False,
            current_url=page.url,
            page_title=_safe_title(page),
            reason="CLICK_FAILED: no application-flow evidence after click",
        )

    return EasyApplyOpenResult(
        opened=True,
        current_url=page.url,
        page_title=_safe_title(page),
        reason="OPENED",
    )


def _poll_for_wizard_opened(page: Page, timeout_seconds: float = 6.0, interval_seconds: float = 0.3) -> bool:
    import time

    deadline = time.monotonic() + timeout_seconds
    while True:
        if _wizard_opened(page):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval_seconds)


def _wizard_opened(page: Page) -> bool:
    # Requires BOTH signals, deliberately -- a URL that happens to contain
    # "job-applications"/"wizard" with no corroborating page content is
    # not trusted alone (a redirect could land there without the flow
    # actually opening).
    #
    # Verified live (Phase 4B.1 CDP-attach closure, 2026-08-21) against a
    # genuinely opened Dice apply flow: there is no wizard-specific
    # data-testid or class anywhere on the real page (the original
    # [class*='apply-wizard'] guess never matched) -- the real page has
    # an exact title "Apply | Dice.com" and a visible "You're Applying
    # for" heading. Both used together, same conservative philosophy.
    url_matches = "job-applications" in page.url and "wizard" in page.url
    dom_matches = (
        _safe_title(page) == "Apply | Dice.com" or page.get_by_text("You're Applying for", exact=False).count() > 0
    )
    return url_matches and dom_matches


def _refuse(page: Page, reason: str, detail: str | None = None) -> EasyApplyOpenResult:
    return EasyApplyOpenResult(
        opened=False,
        current_url=page.url,
        page_title=_safe_title(page),
        reason=reason if detail is None else f"{reason}: {detail}",
    )


def _safe_title(page: Page) -> str:
    try:
        return page.title()
    except Exception:
        return ""
