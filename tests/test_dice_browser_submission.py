"""Phase 5: submission verification. Offline tests only (synthetic HTML,
served via page.route() + page.goto() so relative URLs/history.replaceState
work correctly -- about:blank's cross-origin pushState quirk, same
workaround already used in test_dice_browser_easy_apply.py). No live Dice
needed for any of these.

Most success-path fixtures below still model a plausible shape against
Dice's established UI conventions (headings, role=status regions), not a
live-verified page -- that's why VERIFIED_SUBMITTED requires strong,
scoped evidence and everything weaker falls to VERIFICATION_UNCERTAIN.

Two fixtures ARE live-verified: test_explicit_dice_failure_modal_is_submit_failed
and test_real_dice_success_page_is_verified_submitted. Both come from two
real Submit clicks on job 05fde651-c3ae-40e3-b348-ad1c9e9a6459 (Java
Developer @ Yashnee Tech Solutions) on 2026-08-21 -- the first (before
the onsite question was answered) produced Dice's own explicit failure
modal, "Whoops! There was an issue submitting your application." / "We
were unable to submit your application. Please try again."; the second
(after answering) produced the genuine success page, URL
".../wizard/success", title "Application Success | Dice.com", H2 "Hooray!
Your application is on its way!" No application was submitted in the
first case; the second genuinely was.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright

from dice_browser.models import SubmissionStatus
from dice_browser.submission import SubmitPreconditions, _has_left_wizard, submit_application

JOB_URL = "https://www.dice.com/job-applications/TESTJOB123/wizard"
JOB_FRAGMENT = "TESTJOB123"


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


# ── _has_left_wizard: real live bug regression ────────────────────────────


def test_has_left_wizard_true_for_real_success_url():
    assert _has_left_wizard("https://www.dice.com/job-applications/x/wizard/success") is True


def test_has_left_wizard_false_while_still_on_wizard():
    assert _has_left_wizard("https://www.dice.com/job-applications/x/wizard") is False


def test_has_left_wizard_false_while_still_on_wizard_with_trailing_slash():
    assert _has_left_wizard("https://www.dice.com/job-applications/x/wizard/") is False


def test_has_left_wizard_true_for_unrelated_page():
    assert _has_left_wizard("https://www.dice.com/dashboard/applications") is True


def test_has_left_wizard_ignores_query_string_and_fragment():
    assert _has_left_wizard("https://www.dice.com/job-applications/x/wizard?ref=abc#top") is False


def _clean_preconditions() -> SubmitPreconditions:
    return SubmitPreconditions(authenticated=True, no_unresolved_interventions=True, already_verified_submitted=False)


def _review_page(submit_onclick: str = "", extra_body: str = "") -> str:
    return f"""
    <html><head><title>Apply | Dice.com</title></head><body>
    <nav aria-label="Account"><button>Account</button></nav>
    <div>Step 2 of 2</div>
    <h2>Review your application</h2>
    {extra_body}
    <button>Back</button>
    <button onclick="{submit_onclick}">Submit</button>
    </body></html>
    """


def _load(page, body_html: str) -> None:
    page.route(
        "**/job-applications/TESTJOB123/wizard",
        lambda route: route.fulfill(status=200, content_type="text/html", body=body_html),
    )
    page.goto(JOB_URL)


def _submit(page, **overrides):
    kwargs = dict(
        page=page,
        expected_job_url_fragment=JOB_FRAGMENT,
        application_id="app-1",
        dice_job_id="job-1",
        preconditions=_clean_preconditions(),
        poll_timeout_seconds=0.4,
        poll_interval_seconds=0.1,
    )
    kwargs.update(overrides)
    return submit_application(**kwargs)


# Real live regression (2026-08-21, job 05fde651-c3ae-40e3-b348-ad1c9e9a6459,
# Java Developer @ Yashnee Tech Solutions): a real Submit click produced
# Dice's own explicit failure modal verbatim. Must classify SUBMIT_FAILED,
# not the generic VERIFICATION_UNCERTAIN catch-all -- Dice told us
# directly what happened, this isn't ambiguous.
def test_explicit_dice_failure_modal_is_submit_failed(page):
    onclick = (
        "document.body.innerHTML += "
        "'<div role=\"dialog\">"
        "<h2>Whoops! There was an issue submitting your application.</h2>"
        "<p>We were unable to submit your application. Please try again.</p>"
        "<button>OK</button><button>Go to Search</button><button>Return to Job Details</button>"
        "</div>';"
    )
    _load(page, _review_page(submit_onclick=onclick))
    result = _submit(page, poll_timeout_seconds=1, poll_interval_seconds=0.1)
    assert result.status == SubmissionStatus.SUBMIT_FAILED
    assert "issue submitting your application" in result.evidence["failure_text"].lower()


# Real live regression (2026-08-21, same job, second attempt after
# answering the previously-unanswered onsite question): a real Submit
# click produced Dice's genuine success page. URL became
# ".../wizard/success" -- title "Application Success | Dice.com", visible
# H2 "Hooray! Your application is on its way! \U0001F973". Critical real
# bug this fixture caught: ".../wizard/success" still CONTAINS the
# substring "/wizard", so the original "/wizard" not in after_url check
# would have misclassified a genuine success as still-on-wizard.
def test_real_dice_success_page_is_verified_submitted(page):
    onclick = (
        "history.replaceState({}, '', '/job-applications/TESTJOB123/wizard/success');"
        "document.title = 'Application Success | Dice.com';"
        "document.body.innerHTML = '<h2>Hooray! Your application is on its way!</h2>"
        "<p>You can find the job listing for this role in your Applied Jobs.</p>';"
    )
    _load(page, _review_page(submit_onclick=onclick))
    result = _submit(page, poll_timeout_seconds=2, poll_interval_seconds=0.2)
    assert result.status == SubmissionStatus.VERIFIED_SUBMITTED
    assert "on its way" in result.evidence["confirmation_text"].lower()
    assert "/wizard/success" in result.after_url


# Real live regression (2026-08-21, a DIFFERENT job -- SAP R2R Consultant
# @ MSYS Inc., 6695d2fb-358c-47f4-a9d8-1b22271732bd): a second real
# submission produced the same success shape but with a different
# celebratory prefix -- "Fantastic! Your application is on its way!"
# rather than "Hooray!". Confirms Dice rotates the exclamation but keeps
# the core phrase stable, which is exactly why the match is scoped to
# that stable substring rather than the full sentence.
def test_real_dice_success_page_alternate_wording_is_verified_submitted(page):
    onclick = (
        "history.replaceState({}, '', '/job-applications/TESTJOB123/wizard/success');"
        "document.title = 'Application Success | Dice.com';"
        "document.body.innerHTML = '<h2>Fantastic! Your application is on its way!</h2>"
        "<p>You can find the job listing for this role in your Applied Jobs.</p>';"
    )
    _load(page, _review_page(submit_onclick=onclick))
    result = _submit(page, poll_timeout_seconds=2, poll_interval_seconds=0.2)
    assert result.status == SubmissionStatus.VERIFIED_SUBMITTED
    assert "on its way" in result.evidence["confirmation_text"].lower()


# 1. Submit click alone -> NOT enough
def test_submit_click_alone_is_not_enough(page):
    _load(page, _review_page(submit_onclick=""))
    result = _submit(page)
    assert result.status != SubmissionStatus.VERIFIED_SUBMITTED


# 2. explicit success page -> VERIFIED_SUBMITTED
def test_explicit_success_page_is_verified_submitted(page):
    onclick = (
        "history.replaceState({}, '', '/applications/confirmation');"
        "document.body.innerHTML = '<h1>Application submitted</h1>';"
    )
    _load(page, _review_page(submit_onclick=onclick))
    result = _submit(page, poll_timeout_seconds=2, poll_interval_seconds=0.2)
    assert result.status == SubmissionStatus.VERIFIED_SUBMITTED
    assert "submitted" in result.evidence["confirmation_text"].lower()


# 3. explicit "Application submitted"-family message -> VERIFIED_SUBMITTED
def test_thank_you_message_is_verified_submitted(page):
    onclick = (
        "history.replaceState({}, '', '/applications/confirmation');"
        "document.body.innerHTML = '<h2>Thank you for applying!</h2>';"
    )
    _load(page, _review_page(submit_onclick=onclick))
    result = _submit(page, poll_timeout_seconds=2, poll_interval_seconds=0.2)
    assert result.status == SubmissionStatus.VERIFIED_SUBMITTED


# 4. strong applied-state evidence (role=status) -> VERIFIED_SUBMITTED
def test_status_role_confirmation_is_verified_submitted(page):
    onclick = (
        "history.replaceState({}, '', '/applications/confirmation');"
        "var d=document.createElement('div'); d.setAttribute('role','status');"
        "d.textContent='You have successfully applied'; document.body.appendChild(d);"
    )
    _load(page, _review_page(submit_onclick=onclick))
    result = _submit(page, poll_timeout_seconds=2, poll_interval_seconds=0.2)
    assert result.status == SubmissionStatus.VERIFIED_SUBMITTED


# 5. ambiguous navigation (URL changed, no confirmation text) -> VERIFICATION_UNCERTAIN
def test_ambiguous_navigation_without_confirmation_is_uncertain(page):
    onclick = "history.replaceState({}, '', '/dashboard/applications');"
    _load(page, _review_page(submit_onclick=onclick))
    result = _submit(page)
    assert result.status == SubmissionStatus.VERIFICATION_UNCERTAIN


# 6. Submit disappears but no success evidence -> VERIFICATION_UNCERTAIN
def test_submit_disappears_without_evidence_is_uncertain(page):
    onclick = "document.querySelector('h2').remove(); this.remove();"
    _load(page, _review_page(submit_onclick=onclick))
    result = _submit(page)
    assert result.status == SubmissionStatus.VERIFICATION_UNCERTAIN


# 7. timeout after an inconclusive transition -> VERIFICATION_UNCERTAIN
def test_timeout_with_inconclusive_transition_state_is_uncertain(page):
    onclick = "document.querySelector('h2').remove(); this.remove(); document.body.innerHTML += '<div>Processing...</div>';"
    _load(page, _review_page(submit_onclick=onclick))
    result = _submit(page, poll_timeout_seconds=0.3, poll_interval_seconds=0.1)
    assert result.status == SubmissionStatus.VERIFICATION_UNCERTAIN


# 8. auth page after click -> AUTH_REQUIRED
def test_auth_required_after_submit(page):
    onclick = (
        "history.replaceState({}, '', '/dashboard/login');"
        "document.body.innerHTML = '<input name=\"email\"><button>Login</button>';"
    )
    _load(page, _review_page(submit_onclick=onclick))
    result = _submit(page, poll_timeout_seconds=1, poll_interval_seconds=0.1)
    assert result.status == SubmissionStatus.AUTH_REQUIRED


# 9. security challenge -> SECURITY_CHALLENGE
def test_security_challenge_after_submit(page):
    onclick = "document.body.innerHTML += '<div>Please complete the captcha to continue</div>';"
    _load(page, _review_page(submit_onclick=onclick))
    result = _submit(page, poll_timeout_seconds=1, poll_interval_seconds=0.1)
    assert result.status == SubmissionStatus.SECURITY_CHALLENGE


# 10. application still on Review, click had zero effect -> NOT_SUBMITTED
def test_no_change_after_click_is_not_submitted(page):
    _load(page, _review_page(submit_onclick=""))
    result = _submit(page, poll_timeout_seconds=0.2, poll_interval_seconds=0.1)
    assert result.status == SubmissionStatus.NOT_SUBMITTED


# 11. no false positive from unrelated word "submitted"
def test_unrelated_submitted_word_does_not_false_positive(page):
    extra_body = "<h2>Documents submitted</h2>"  # doesn't match any of our anchored phrases
    _load(page, _review_page(submit_onclick="", extra_body=extra_body))
    result = _submit(page, poll_timeout_seconds=0.2, poll_interval_seconds=0.1)
    assert result.status != SubmissionStatus.VERIFIED_SUBMITTED


# 12. no generic text substring matching -- scoping itself, not just phrasing
def test_generic_body_text_does_not_count_as_confirmation(page):
    extra_body = "<div>Note: this application submitted its documents to our internal queue.</div>"
    _load(page, _review_page(submit_onclick="", extra_body=extra_body))
    result = _submit(page, poll_timeout_seconds=0.2, poll_interval_seconds=0.1)
    assert result.status != SubmissionStatus.VERIFIED_SUBMITTED


# 13. application/job identity preserved
def test_application_and_job_identity_preserved_in_result(page):
    _load(page, _review_page(submit_onclick=""))
    result = _submit(page, application_id="app-xyz", dice_job_id="job-abc")
    assert result.application_id == "app-xyz"
    assert result.dice_job_id == "job-abc"


# 14. success evidence scoped to the actual submission (before/after URLs
# on the evidence match the real navigation, not fabricated or copied
# from an unrelated context). Known limitation, not hidden: a heading
# mentioning "submitted" inside an unrelated same-page widget (e.g. a
# "similar jobs" sidebar) would currently still match -- no live evidence
# exists yet to design a tighter container-scoped check, so this is
# flagged for revisit once a real confirmation page is observed.
def test_evidence_urls_scoped_to_this_submission(page):
    onclick = (
        "history.replaceState({}, '', '/applications/confirmation');"
        "document.body.innerHTML = '<h1>Application submitted</h1>';"
    )
    _load(page, _review_page(submit_onclick=onclick))
    result = _submit(page, poll_timeout_seconds=2, poll_interval_seconds=0.2)
    assert JOB_FRAGMENT in result.before_url
    assert result.evidence["before_url"] == result.before_url
    assert result.evidence["after_url"] == result.after_url


# 15. no duplicate submission attempt if state already verified submitted
def test_already_verified_submitted_short_circuits_without_clicking(page):
    onclick = "window.__clicked = true;"
    _load(page, _review_page(submit_onclick=onclick))
    preconditions = SubmitPreconditions(authenticated=True, no_unresolved_interventions=True, already_verified_submitted=True)
    result = _submit(page, preconditions=preconditions)
    assert result.status == SubmissionStatus.VERIFIED_SUBMITTED
    assert page.evaluate("window.__clicked") is not True


# ── pre-submit gate ────────────────────────────────────────────────────────


def test_gate_refuses_when_not_authenticated(page):
    _load(page, _review_page(submit_onclick="window.__clicked = true;"))
    preconditions = SubmitPreconditions(authenticated=False, no_unresolved_interventions=True, already_verified_submitted=False)
    result = _submit(page, preconditions=preconditions)
    assert result.status == SubmissionStatus.AUTH_REQUIRED
    assert page.evaluate("window.__clicked") is not True


def test_gate_refuses_when_unresolved_interventions(page):
    _load(page, _review_page(submit_onclick="window.__clicked = true;"))
    preconditions = SubmitPreconditions(authenticated=True, no_unresolved_interventions=False, already_verified_submitted=False)
    result = _submit(page, preconditions=preconditions)
    assert result.status == SubmissionStatus.NEEDS_INPUT
    assert page.evaluate("window.__clicked") is not True


def test_gate_refuses_on_security_challenge_before_click(page):
    extra_body = "<div>Please complete the captcha to continue</div>"
    _load(page, _review_page(submit_onclick="window.__clicked = true;", extra_body=extra_body))
    result = _submit(page)
    assert result.status == SubmissionStatus.SECURITY_CHALLENGE
    assert page.evaluate("window.__clicked") is not True


def test_gate_refuses_when_url_does_not_match_expected_job(page):
    _load(page, _review_page(submit_onclick="window.__clicked = true;"))
    result = _submit(page, expected_job_url_fragment="SOME-OTHER-JOB-ID")
    assert result.status == SubmissionStatus.SUBMIT_FAILED
    assert page.evaluate("window.__clicked") is not True
