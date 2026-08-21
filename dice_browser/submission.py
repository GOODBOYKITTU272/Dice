"""Phase 5: the ONLY module in this codebase permitted to click Submit on
a Dice application -- gated behind explicit preconditions, exactly like
Phase 4C's easy_apply.py is the only module permitted past
/job-applications/...

Core rule this whole module exists to enforce: clicking Submit is never
itself evidence of success. Button-click-succeeded, navigation-occurred,
button-disappeared, HTTP 200, generic page text, and "the poll finished
without erroring" are all explicitly NOT accepted as proof -- only a
scoped, explicit positive confirmation signal (heading/status-role text
matching a known confirmation phrase) combined with the URL actually
leaving the wizard counts as VERIFIED_SUBMITTED. Anything weaker is
VERIFICATION_UNCERTAIN, never guessed upward into success.

No live "Submit clicked -> confirmed success" case has been observed yet
as of this module's creation (2026-08-21) -- every confirmation phrase
and signal below is a best-effort design against Dice's established UI
conventions (headings, role=status/alert regions, the same wizard-URL
pattern used everywhere else in this codebase), not a live-verified
selector. `.ribbon-status-applied` in particular is carried over from the
Phase 4A reference-repo audit as a *concept* only -- it has never been
seen on a real already-applied Dice page and is deliberately NOT used as
a strong signal here.

This module never touches Supabase -- see db/submission_repository.py for
the separate, explicit DB-side state transition (SUBMITTED is written
only after this module returns VERIFIED_SUBMITTED).
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from playwright.sync_api import Page

from dice_browser.models import BrowserState, SubmissionResult, SubmissionStatus
from dice_browser.questions import is_review_screen
from dice_browser.session import classify_authentication, detect_challenge

_CONFIRMATION_PHRASES = (
    "application submitted",
    "your application has been submitted",
    "thank you for applying",
    "you've successfully applied",
    "you have successfully applied",
    "you're all set",
)

# Live-verified (2026-08-21, job 05fde651-c3ae-40e3-b348-ad1c9e9a6459, Java
# Developer @ Yashnee Tech Solutions): a real Submit click produced this
# modal verbatim -- "Whoops! There was an issue submitting your
# application." / "We were unable to submit your application. Please try
# again." Unlike the confirmation phrases above, this one is not a guess.
_FAILURE_PHRASES = (
    "there was an issue submitting your application",
    "we were unable to submit your application",
    "unable to submit your application",
)

_EVIDENCE_SELECTOR = "h1, h2, h3, [role='status'], [role='alert'], [role='dialog']"


@dataclass
class SubmitPreconditions:
    """Orchestration-level facts only a future worker can know (Supabase
    intervention/application state) -- this module has no DB access, so
    it can't check these itself. It still independently re-verifies
    everything actually observable from the live page (auth, Review
    screen, security challenge, correct job) before ever clicking."""

    authenticated: bool
    no_unresolved_interventions: bool
    already_verified_submitted: bool


def _result(
    status: SubmissionStatus,
    reason: str,
    evidence: dict,
    application_id: str | None,
    dice_job_id: str | None,
    before_url: str,
    after_url: str | None = None,
) -> SubmissionResult:
    return SubmissionResult(
        status=status,
        reason=reason,
        evidence=evidence,
        application_id=application_id,
        dice_job_id=dice_job_id,
        before_url=before_url,
        after_url=after_url if after_url is not None else before_url,
    )


def _scoped_text_matching(page: Page, phrases: tuple[str, ...]) -> str | None:
    """Scoped to heading/status/alert/dialog elements only -- never a
    page-wide substring search. A candidate's own free-text answer, a job
    description, or an unrelated UI string containing a phrase-like
    substring must never false-positive this."""
    for el in page.locator(_EVIDENCE_SELECTOR).all():
        try:
            if not el.is_visible():
                continue
            text = el.inner_text().strip()
        except Exception:
            continue
        lowered = text.lower()
        for phrase in phrases:
            if phrase in lowered:
                return text
    return None


def _poll_for_evidence(page: Page, timeout_seconds: float, interval_seconds: float) -> tuple[str | None, str | None]:
    """Returns (confirmation_text, failure_text) -- whichever appears
    first, or (None, None) if neither shows up within the window."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        failure = _scoped_text_matching(page, _FAILURE_PHRASES)
        confirmation = _scoped_text_matching(page, _CONFIRMATION_PHRASES)
        if failure or confirmation:
            return confirmation, failure
        if time.monotonic() >= deadline:
            return None, None
        try:
            page.wait_for_timeout(interval_seconds * 1000)
        except Exception:
            pass


def submit_application(
    page: Page,
    expected_job_url_fragment: str,
    application_id: str | None,
    dice_job_id: str | None,
    preconditions: SubmitPreconditions,
    poll_timeout_seconds: float = 10.0,
    poll_interval_seconds: float = 0.5,
) -> SubmissionResult:
    """Clicks Submit exactly once, only if every gate passes, then
    classifies the result from bounded post-submit evidence. Never
    retries, never clicks twice in one call, never guesses success from
    weak signals."""
    before_url = page.url

    if preconditions.already_verified_submitted:
        return _result(
            SubmissionStatus.VERIFIED_SUBMITTED,
            "already verified submitted -- refusing a duplicate submit attempt",
            {},
            application_id,
            dice_job_id,
            before_url,
        )

    if not preconditions.authenticated:
        return _result(SubmissionStatus.AUTH_REQUIRED, "caller reports not authenticated", {}, application_id, dice_job_id, before_url)

    if not preconditions.no_unresolved_interventions:
        return _result(
            SubmissionStatus.NEEDS_INPUT,
            "caller reports unresolved interventions -- refusing to submit",
            {},
            application_id,
            dice_job_id,
            before_url,
        )

    if classify_authentication(page) != BrowserState.ACTIVE:
        return _result(SubmissionStatus.AUTH_REQUIRED, "live page is not authenticated", {}, application_id, dice_job_id, before_url)

    challenge = detect_challenge(page)
    if challenge is not None:
        return _result(
            SubmissionStatus.SECURITY_CHALLENGE,
            "security challenge present before submit",
            {"challenge_type": challenge.value},
            application_id,
            dice_job_id,
            before_url,
        )

    if expected_job_url_fragment not in before_url:
        return _result(
            SubmissionStatus.SUBMIT_FAILED,
            "current page URL does not match the expected application/job",
            {},
            application_id,
            dice_job_id,
            before_url,
        )

    if not is_review_screen(page):
        return _result(SubmissionStatus.SUBMIT_FAILED, "page is not a recognized Review screen", {}, application_id, dice_job_id, before_url)

    submit_button = page.get_by_role("button", name="Submit", exact=False)
    if submit_button.count() != 1:
        return _result(
            SubmissionStatus.SUBMIT_FAILED, "Submit button not uniquely identified", {}, application_id, dice_job_id, before_url
        )
    if not submit_button.first.is_visible() or submit_button.first.get_attribute("disabled") is not None:
        return _result(
            SubmissionStatus.SUBMIT_FAILED, "Submit button not safely clickable", {}, application_id, dice_job_id, before_url
        )

    submit_button.first.click()

    confirmation_text, failure_text = _poll_for_evidence(page, poll_timeout_seconds, poll_interval_seconds)
    return _classify_post_submit(page, before_url, confirmation_text, failure_text, application_id, dice_job_id)


def _classify_post_submit(
    page: Page,
    before_url: str,
    confirmation_text: str | None,
    failure_text: str | None,
    application_id: str | None,
    dice_job_id: str | None,
) -> SubmissionResult:
    after_url = page.url

    challenge = detect_challenge(page)
    if challenge is not None:
        return _result(
            SubmissionStatus.SECURITY_CHALLENGE,
            "security challenge appeared after submit",
            {"challenge_type": challenge.value},
            application_id,
            dice_job_id,
            before_url,
            after_url,
        )

    if classify_authentication(page) == BrowserState.AUTH_REQUIRED:
        return _result(
            SubmissionStatus.AUTH_REQUIRED,
            "session no longer authenticated after submit",
            {},
            application_id,
            dice_job_id,
            before_url,
            after_url,
        )

    if failure_text:
        # Live-verified (2026-08-21): Dice's own explicit failure modal.
        # Checked before the confirmation-text branch on purpose -- an
        # explicit negative signal must never be outweighed by a weaker
        # positive one.
        return _result(
            SubmissionStatus.SUBMIT_FAILED,
            "Dice explicitly reported a submission failure",
            {"failure_text": failure_text},
            application_id,
            dice_job_id,
            before_url,
            after_url,
        )

    left_wizard = "/wizard" not in after_url

    if confirmation_text and left_wizard:
        return _result(
            SubmissionStatus.VERIFIED_SUBMITTED,
            "explicit confirmation text found and the page left the wizard",
            {"confirmation_text": confirmation_text, "before_url": before_url, "after_url": after_url},
            application_id,
            dice_job_id,
            before_url,
            after_url,
        )

    if confirmation_text and not left_wizard:
        return _result(
            SubmissionStatus.VERIFICATION_UNCERTAIN,
            "confirmation text found but the URL is still on the wizard -- ambiguous",
            {"confirmation_text": confirmation_text},
            application_id,
            dice_job_id,
            before_url,
            after_url,
        )

    if left_wizard and not confirmation_text:
        return _result(
            SubmissionStatus.VERIFICATION_UNCERTAIN,
            "URL left the wizard but no explicit confirmation text was found",
            {},
            application_id,
            dice_job_id,
            before_url,
            after_url,
        )

    still_review = is_review_screen(page)
    submit_still_present = page.get_by_role("button", name="Submit", exact=False).count() > 0

    if still_review and submit_still_present:
        return _result(
            SubmissionStatus.NOT_SUBMITTED,
            "still on the Review screen with Submit visible -- the click had no effect",
            {},
            application_id,
            dice_job_id,
            before_url,
            after_url,
        )

    return _result(
        SubmissionStatus.VERIFICATION_UNCERTAIN,
        "no positive or negative evidence found within the poll window",
        {},
        application_id,
        dice_job_id,
        before_url,
        after_url,
    )
