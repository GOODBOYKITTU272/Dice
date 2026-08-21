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
        pass  # best-effort settle; evidence check below is what actually decides success

    if not _wizard_opened(page):
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


def _wizard_opened(page: Page) -> bool:
    # Requires BOTH signals, deliberately -- a URL that happens to contain
    # "job-applications"/"wizard" with no corroborating page content is
    # not trusted alone (a redirect could land there without the flow
    # actually opening). Exact production DOM selector is a placeholder
    # pending live-DOM confirmation once the Phase 4B.1 auth prerequisite
    # is completed (see STATE.md) -- requiring both keeps this
    # conservative in the meantime: CLICK_FAILED, never a guessed OPENED.
    url_matches = "job-applications" in page.url and "wizard" in page.url
    dom_matches = page.locator("[data-testid*='apply-wizard'], [class*='apply-wizard']").count() > 0
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
