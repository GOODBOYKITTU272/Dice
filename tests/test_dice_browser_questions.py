"""Phase 4D-A: Review-screen / no-questions detection only. Offline tests
(synthetic HTML); no live Dice needed for any of these. The real
'Review your application' fixture below mirrors the live-verified shape
observed on job 469efdf8-e321-46a1-9346-70870d020736 (Data Engineer @
Stefanini, 2026-08-21): Step 2 of 2, a read-only profile-summary review,
Back/Submit buttons, and exactly one unrelated hidden control.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright

from dice_browser.models import QuestionExtractionStatus
from dice_browser.questions import extract_questions, is_review_screen


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


_REVIEW_SCREEN_SHELL = """
<html><body>
<div>Step 2 of 2</div>
<h2>Review your application</h2>
{extra}
<button>Back</button>
<button>Submit</button>
</body></html>
"""

_REAL_DISPLAY_CARDS = """
<div class="self-stretch"><p>Resume *</p><p>resume.pdf</p></div>
<div class="self-stretch"><p>Cover Letter</p><p>test_resume.pdf</p></div>
<div class="self-stretch"><p>Work Authorization *</p><p>US Citizen</p></div>
<div class="self-stretch"><p>Current Location *</p><p>Some City, ST</p></div>
<div><p>Dice Job Match Score&trade;</p><p>85%</p></div>
<div><p>Profile Visibility: Off</p></div>
<input type="checkbox" role="switch" style="display:none">
"""


def _review_page(extra: str) -> str:
    return _REVIEW_SCREEN_SHELL.format(extra=extra)


# ── real live shape: zero questions ───────────────────────────────────────


def test_review_screen_with_no_controls_returns_no_questions_present(page):
    page.set_content(_review_page(_REAL_DISPLAY_CARDS))
    result = extract_questions(page)
    assert result.status == QuestionExtractionStatus.NO_QUESTIONS_PRESENT
    assert result.questions == ()


def test_display_only_work_authorization_not_extracted_as_question(page):
    page.set_content(_review_page('<div class="self-stretch"><p>Work Authorization *</p><p>US Citizen</p></div>'))
    result = extract_questions(page)
    assert result.status == QuestionExtractionStatus.NO_QUESTIONS_PRESENT
    assert result.questions == ()


def test_display_only_current_location_not_extracted_as_question(page):
    page.set_content(_review_page('<div class="self-stretch"><p>Current Location *</p><p>Some City, ST</p></div>'))
    result = extract_questions(page)
    assert result.status == QuestionExtractionStatus.NO_QUESTIONS_PRESENT
    assert result.questions == ()


def test_hidden_unrelated_checkbox_ignored(page):
    page.set_content(_review_page('<input type="checkbox" role="switch" style="display:none">'))
    result = extract_questions(page)
    assert result.status == QuestionExtractionStatus.NO_QUESTIONS_PRESENT
    assert result.questions == ()


def test_submit_button_presence_does_not_count_as_a_question(page):
    # The review shell itself always has Back + Submit -- with no other
    # content at all, this must still be NO_QUESTIONS_PRESENT.
    page.set_content(_review_page(""))
    result = extract_questions(page)
    assert result.status == QuestionExtractionStatus.NO_QUESTIONS_PRESENT
    assert result.questions == ()


def test_resume_and_cover_letter_summary_cards_ignored(page):
    page.set_content(
        _review_page(
            '<div class="self-stretch"><p>Resume *</p><p>resume.pdf</p></div>'
            '<div class="self-stretch"><p>Cover Letter</p><p>test_resume.pdf</p></div>'
        )
    )
    result = extract_questions(page)
    assert result.status == QuestionExtractionStatus.NO_QUESTIONS_PRESENT
    assert result.questions == ()


# ── real controls present: must not collapse to NO_QUESTIONS_PRESENT ─────


def test_page_with_real_question_control_does_not_return_no_questions_present(page):
    page.set_content(_review_page('<label>Years of experience</label><input type="text" id="yoe">'))
    result = extract_questions(page)
    assert result.status != QuestionExtractionStatus.NO_QUESTIONS_PRESENT
    assert result.status == QuestionExtractionStatus.QUESTIONS_PRESENT
    assert len(result.questions) == 1


def test_unsupported_visible_control_not_silently_dropped(page):
    # A custom, non-native widget (role=combobox on a div, not a real
    # <select>) -- still must be counted, never silently ignored just
    # because it isn't a plain native control.
    page.set_content(_review_page('<div role="combobox" tabindex="0">Choose one</div>'))
    result = extract_questions(page)
    assert result.status != QuestionExtractionStatus.NO_QUESTIONS_PRESENT
    assert len(result.questions) == 1


# ── review-screen detection itself: explicit and deterministic ───────────


def test_is_review_screen_true_for_real_shape(page):
    page.set_content(_review_page(_REAL_DISPLAY_CARDS))
    assert is_review_screen(page) is True


def test_is_review_screen_false_for_step_1_resume_page(page):
    page.set_content(
        """
        <html><body>
        <div>Step 1 of 2</div>
        <p>Resume *</p>
        <p>resume.pdf</p>
        <button>Next</button>
        </body></html>
        """
    )
    assert is_review_screen(page) is False


def test_extract_questions_never_guesses_on_non_review_screen(page):
    # A completely unrelated page must not be reported as "no questions" --
    # that would be indistinguishable from a real, verified empty review.
    page.set_content("<html><body><p>Some other page entirely.</p></body></html>")
    result = extract_questions(page)
    assert result.status == QuestionExtractionStatus.UNKNOWN_SCREEN
    assert result.status != QuestionExtractionStatus.NO_QUESTIONS_PRESENT
