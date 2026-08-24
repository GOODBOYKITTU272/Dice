"""Phase 4B: opens one already-discovered Dice job URL and inspects safety
signals. Never runs Dice's own search UI — dice/search.py's HTTP discovery
already produces each job's canonical_url and remains authoritative for
that. Never clicks Easy Apply, Next, Submit, or anything else; this module
is pure inspection.
"""
from __future__ import annotations

from urllib.parse import urlparse

from playwright.sync_api import Page

from dice_browser.models import BrowserState, NavigationResult
from dice_browser.session import classify_authentication, detect_challenge

_ALLOWED_HOST = "www.dice.com"
_ALLOWED_PATH_PREFIX = "/job-detail/"
# Never navigate anywhere under this prefix — application-initiation is
# categorically out of scope for discovery/foundation code (see STATE.md).
_FORBIDDEN_PATH_PREFIXES = ("/job-applications/",)


class InvalidJobUrlError(ValueError):
    pass


def validate_canonical_url(canonical_url: str) -> None:
    parsed = urlparse(canonical_url)
    if parsed.scheme != "https" or parsed.netloc != _ALLOWED_HOST:
        raise InvalidJobUrlError(f"Refusing to navigate: {canonical_url!r} is not a www.dice.com URL")
    if any(parsed.path.startswith(p) for p in _FORBIDDEN_PATH_PREFIXES):
        raise InvalidJobUrlError(
            f"Refusing to navigate: {canonical_url!r} is an application-initiation path"
        )
    if not parsed.path.startswith(_ALLOWED_PATH_PREFIX):
        raise InvalidJobUrlError(
            f"Refusing to navigate: {canonical_url!r} is not a recognized job-detail URL"
        )


def open_job(page: Page, canonical_url: str) -> NavigationResult:
    validate_canonical_url(canonical_url)

    page.goto(canonical_url, wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass  # best-effort settle; a slow third-party widget shouldn't fail the whole navigation

    challenge = detect_challenge(page)
    if challenge is not None:
        return NavigationResult(
            canonical_url=canonical_url,
            page_title=_safe_title(page),
            browser_state=BrowserState.NEEDS_INPUT,
            authenticated=False,
            already_applied=None,
            easy_apply_visible=None,
            challenge_type=challenge,
            evidence=f"security challenge detected: {challenge.value}",
        )

    auth_state = classify_authentication(page)
    reloaded_for_auth = False
    if auth_state == BrowserState.AUTH_REQUIRED:
        # Real root cause, live-found 2026-08-24/25: a brand-new browser
        # context (exactly what a fresh Browserless connect gives every
        # claim) reliably shows a logged-out header on the FIRST paint
        # even with genuinely valid, unexpired cookies -- a client-side
        # hydration race on Dice's own frontend, not an actually dead
        # session (confirmed: the token's own inactivity_exp claim was
        # still hours away every time this was live-reproduced). A
        # single reload consistently resolves it. This was very likely
        # the real cause behind most/all of tonight's AUTH_REQUIRED
        # stops -- retried here once, cheaply, before ever concluding
        # the session is genuinely dead.
        page.reload(wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        auth_state = classify_authentication(page)
        reloaded_for_auth = True

    authenticated = auth_state == BrowserState.ACTIVE
    easy_apply_visible = _detect_easy_apply(page)
    # Already-applied is inherently a per-account signal — Dice can't show
    # it to a logged-out (or ambiguous-state) visitor, so it's unknown
    # (None), never guessed False, unless we're confirmed authenticated.
    already_applied = _detect_already_applied(page) if authenticated else None

    if auth_state == BrowserState.NEEDS_INPUT:
        evidence = "auth signals ambiguous or conflicting — never guessed"
    elif reloaded_for_auth:
        evidence = f"page loaded and inspected; auth state resolved via one reload retry ({auth_state.value})"
    else:
        evidence = "page loaded and inspected; no security challenge detected"
    return NavigationResult(
        canonical_url=canonical_url,
        page_title=_safe_title(page),
        browser_state=auth_state,
        authenticated=authenticated,
        already_applied=already_applied,
        easy_apply_visible=easy_apply_visible,
        challenge_type=None,
        evidence=evidence,
    )


def _safe_title(page: Page) -> str:
    try:
        return page.title()
    except Exception:
        return ""


def _detect_already_applied(page: Page) -> bool:
    # Reference concept from the Phase 4A audit (AndrewKassab/Dice-AI):
    # ribbon-status-applied. Kept as a fallback in case Dice reintroduces
    # it on some page variant, but live verification (2026-08-23, Steel
    # compatibility spike) found current Dice markup no longer emits
    # that class at all on a job we know was actually submitted.
    if page.locator(".ribbon-status-applied").count() > 0:
        return True
    # Current signal (live-verified 2026-08-23 against a real,
    # previously-submitted job): the same button element that shows
    # "Apply Now"/"Easy Apply" before applying (data-testid="apply-button")
    # instead renders disabled with the exact accessible name "Applied".
    # A first attempt at this fix tried a loose page-wide text search for
    # "You applied" and got a real false positive from unrelated "Create
    # a job alert" marketing copy ("...the job you applied for") that
    # appears on every job page regardless of application status --
    # exactly the failure mode this project was warned to avoid.
    # get_by_role with exact=True matches only an element whose ENTIRE
    # accessible name is "Applied", never a substring within other text.
    return page.get_by_role("button", name="Applied", exact=True).count() > 0


def _detect_easy_apply(page: Page) -> bool:
    # Confirmation-only re-check — dice/easy_apply_detector.py's HTTP-based
    # signal remains the primary qualification source.
    #
    # apply-button-wc (the Phase 4A reference locator, independently used
    # by all 3 audited repos) is checked first but is NOT trusted alone:
    # live validation during this phase found current Dice markup no
    # longer emits that custom element at all (confirmed by direct page
    # inspection, 2026-08-21) — a real example of the "Dice's DOM can
    # change without notice" risk the Phase 4A audit flagged as
    # hypothetical. The proven-reliable signal (20/20 real jobs matched,
    # 0 false positives/negatives, Phase 3B live validation) is the Apply
    # link's own href: /job-applications/{id}/wizard is Dice's native Easy
    # Apply flow; /job-applications/{id}/start-apply is not. This is a
    # read-only attribute read, never a navigation or click.
    if page.locator("apply-button-wc").count() > 0:
        return True
    # Matched on the un-slashed tokens, not "/job-applications/" or
    # "/wizard" with literal slashes: live Dice pages route the Apply
    # link through a login redirect whose query string percent-encodes
    # the slashes (redirectUrl=%2Fjob-applications%2F{id}%2Fwizard),
    # confirmed by direct inspection of the real DOM during this phase.
    apply_link = page.locator("a[href*='job-applications']").first
    if apply_link.count() == 0:
        return False
    href = apply_link.get_attribute("href") or ""
    return "wizard" in href
