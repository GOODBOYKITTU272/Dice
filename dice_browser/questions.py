"""Phase 4D-A: Review-screen / no-questions detection only. No question
answering, no field filling, no Next/Continue/Back/Submit -- those don't
exist anywhere in this module, and this phase doesn't build them.

Only one real live question-flow branch has been observed so far (Data
Engineer @ Stefanini, 469efdf8-e321-46a1-9346-70870d020736, 2026-08-21):
Step 2 of 2 is a read-only "Review your application" screen with zero
fillable question controls -- Work Authorization and Current Location are
pulled from the candidate's existing profile and shown for confirmation,
not asked fresh. A real screening-question screen (radio/select/text/
checkbox controls) has not been observed live yet, so this module
deliberately does not attempt to classify or extract prompts for whatever
controls it does find -- see QUESTIONS_PRESENT in models.py.
"""
from __future__ import annotations

from playwright.sync_api import Page

from dice_browser.models import FieldType, QuestionExtractionResult, QuestionExtractionStatus, QuestionField

_REVIEW_HEADING_TEXT = "Review your application"
_STEP_2_OF_2_TEXT = "Step 2 of 2"

_CANDIDATE_CONTROL_SELECTOR = "input, select, textarea, [role='radio'], [role='checkbox'], [role='combobox']"


def is_review_screen(page: Page) -> bool:
    """Explicit, deterministic Review-screen detection -- live-verified
    shape (2026-08-21): the step indicator, the review heading, and a
    Submit button must ALL be present. No single signal is trusted alone:
    the heading text could in principle appear elsewhere, and a bare
    Submit button is too generic to mean anything by itself."""
    body_text = page.inner_text("body")
    has_step_2 = _STEP_2_OF_2_TEXT in body_text
    has_heading = _REVIEW_HEADING_TEXT in body_text
    has_submit = page.get_by_role("button", name="Submit", exact=False).count() > 0
    return has_step_2 and has_heading and has_submit


def _find_candidate_controls(page: Page):
    """Visible input/select/textarea/ARIA-widget elements only. Never
    treats a hidden control as a question -- the real live page's one
    control (an unlabeled, unrelated hidden checkbox) must never surface
    as a screening question."""
    return [el for el in page.locator(_CANDIDATE_CONTROL_SELECTOR).all() if el.is_visible()]


def extract_questions(page: Page) -> QuestionExtractionResult:
    """Detection/extraction only -- never clicks Submit, Back, Next, or
    Continue, never fills or selects anything. If the page isn't a
    recognized Review screen, this refuses to guess whether questions are
    present (UNKNOWN_SCREEN) rather than defaulting to
    NO_QUESTIONS_PRESENT. Any visible candidate control found (even one
    this module can't yet classify) reports QUESTIONS_PRESENT -- never
    silently dropped."""
    if not is_review_screen(page):
        return QuestionExtractionResult(status=QuestionExtractionStatus.UNKNOWN_SCREEN, questions=())

    candidates = _find_candidate_controls(page)
    if not candidates:
        return QuestionExtractionResult(status=QuestionExtractionStatus.NO_QUESTIONS_PRESENT, questions=())

    questions = tuple(
        QuestionField(question_id=f"unclassified-{i}", field_type=FieldType.UNSUPPORTED)
        for i in range(len(candidates))
    )
    return QuestionExtractionResult(status=QuestionExtractionStatus.QUESTIONS_PRESENT, questions=questions)
