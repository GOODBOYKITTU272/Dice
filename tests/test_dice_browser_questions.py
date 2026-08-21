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

from dice_browser.models import FieldType, QuestionExtractionStatus, QuestionStatus, RequiredState
from dice_browser.questions import extract_questions, is_questions_screen, is_review_screen


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


def test_is_review_screen_generalizes_beyond_step_2_of_2(page):
    # Model correction: Yashnee's real wizard has 3 steps, not 2, proving
    # step counts vary -- the review-screen signal must key off the
    # heading + Submit combination, not a literal "Step 2 of 2" string.
    page.set_content(
        """
        <html><body>
        <div>Step 3 of 3</div>
        <h2>Review your application</h2>
        <button>Back</button>
        <button>Submit</button>
        </body></html>
        """
    )
    assert is_review_screen(page) is True


# ── real live shape: Application Questions screen (Yashnee Tech Solutions,
# job 3f63223a-1dc9-4af9-914c-4ed01e625d44, Step 2 of 3, 2026-08-21) ──────


_QUESTIONS_SCREEN_SHELL = """
<html><body>
<div>Step 2 of 3</div>
<h2>Application Questions</h2>
{extra}
<button>Back</button>
<button>Next</button>
</body></html>
"""


def _questions_page(extra: str) -> str:
    return _QUESTIONS_SCREEN_SHELL.format(extra=extra)


_ONSITE_RADIOGROUP_FIXTURE = """
<div>
  <p id="rg-label">Are you able and willing to regularly come into the office to work?</p>
  <p id="rg-desc">This job requires you to be on-site on a regular basis. By selecting "Yes", you confirm that your can meet this requirement.</p>
  <div role="radiogroup" id="react-aria-generated-1" aria-labelledby="rg-label" aria-describedby="rg-desc">
    <label><input type="radio" name="c59c9cd9-8441-4610-8e13-2621ae1669c2" value="yes"> Yes</label>
    <label><input type="radio" name="c59c9cd9-8441-4610-8e13-2621ae1669c2" value="no"> No</label>
  </div>
</div>
"""

_SALARY_TEXTAREA_FIXTURE = """
<div>
  <p id="ta-label">What is your expected rate or salary?</p>
  <textarea id="react-aria-generated-2" aria-labelledby="ta-label" name="96824b6c-c489-4500-9dcc-d82847b7b1b3" placeholder="Your answer here..."></textarea>
  <p>Enter your desired hourly rate or annual salary. For contract roles, specify hourly; for full-time positions, specify annual.</p>
  <span>0/1000</span>
</div>
"""

_HIDDEN_SWITCH_CHECKBOX = '<input type="checkbox" role="switch" style="display:none">'


def test_is_questions_screen_true_for_real_shape(page):
    page.set_content(_questions_page(_ONSITE_RADIOGROUP_FIXTURE))
    assert is_questions_screen(page) is True


def test_is_questions_screen_false_for_review_page(page):
    page.set_content(_review_page(_REAL_DISPLAY_CARDS))
    assert is_questions_screen(page) is False


# 1. real-shaped yes/no radiogroup extraction
def test_radiogroup_extraction_real_shape(page):
    page.set_content(_questions_page(_ONSITE_RADIOGROUP_FIXTURE))
    result = extract_questions(page)
    assert result.status == QuestionExtractionStatus.QUESTIONS_PRESENT
    assert len(result.questions) == 1
    assert result.questions[0].field_type == FieldType.RADIO


# 2. shared radio name becomes question_id
def test_radiogroup_question_id_from_shared_name(page):
    page.set_content(_questions_page(_ONSITE_RADIOGROUP_FIXTURE))
    q = extract_questions(page).questions[0]
    assert q.question_id == "c59c9cd9-8441-4610-8e13-2621ae1669c2"


# 3. prompt via aria-labelledby
def test_radiogroup_prompt_via_aria_labelledby(page):
    page.set_content(_questions_page(_ONSITE_RADIOGROUP_FIXTURE))
    q = extract_questions(page).questions[0]
    assert q.prompt == "Are you able and willing to regularly come into the office to work?"


# 4. helper via aria-describedby
def test_radiogroup_helper_via_aria_describedby(page):
    page.set_content(_questions_page(_ONSITE_RADIOGROUP_FIXTURE))
    q = extract_questions(page).questions[0]
    assert q.helper is not None and "on-site on a regular basis" in q.helper


# 5. Yes/No options captured
def test_radiogroup_options_captured(page):
    page.set_content(_questions_page(_ONSITE_RADIOGROUP_FIXTURE))
    q = extract_questions(page).questions[0]
    assert q.options == ("Yes", "No")


# 6. unanswered radiogroup state
def test_radiogroup_unanswered_state_and_needs_input(page):
    page.set_content(_questions_page(_ONSITE_RADIOGROUP_FIXTURE))
    q = extract_questions(page).questions[0]
    assert q.current_value is None
    assert q.status == QuestionStatus.NEEDS_INPUT


# 7. textarea extraction
def test_textarea_extraction_real_shape(page):
    page.set_content(_questions_page(_SALARY_TEXTAREA_FIXTURE))
    result = extract_questions(page)
    assert result.status == QuestionExtractionStatus.QUESTIONS_PRESENT
    assert len(result.questions) == 1
    assert result.questions[0].field_type == FieldType.TEXTAREA


# 8. textarea name becomes question_id
def test_textarea_question_id_from_name(page):
    page.set_content(_questions_page(_SALARY_TEXTAREA_FIXTURE))
    q = extract_questions(page).questions[0]
    assert q.question_id == "96824b6c-c489-4500-9dcc-d82847b7b1b3"


# 9. textarea prompt via aria-labelledby
def test_textarea_prompt_via_aria_labelledby(page):
    page.set_content(_questions_page(_SALARY_TEXTAREA_FIXTURE))
    q = extract_questions(page).questions[0]
    assert q.prompt == "What is your expected rate or salary?"


# 10. adjacent helper without aria-describedby
def test_textarea_adjacent_helper_without_aria_describedby(page):
    page.set_content(_questions_page(_SALARY_TEXTAREA_FIXTURE))
    q = extract_questions(page).questions[0]
    assert q.helper == (
        "Enter your desired hourly rate or annual salary. For contract roles, "
        "specify hourly; for full-time positions, specify annual."
    )


# 11. empty textarea current state
def test_textarea_empty_current_state(page):
    page.set_content(_questions_page(_SALARY_TEXTAREA_FIXTURE))
    q = extract_questions(page).questions[0]
    assert q.current_value is None
    assert q.status == QuestionStatus.NEEDS_INPUT


# 12. required_state UNKNOWN when Dice exposes no evidence
def test_required_state_unknown_for_both_live_questions(page):
    page.set_content(_questions_page(_ONSITE_RADIOGROUP_FIXTURE + _SALARY_TEXTAREA_FIXTURE))
    result = extract_questions(page)
    assert len(result.questions) == 2
    for q in result.questions:
        assert q.required_state == RequiredState.UNKNOWN


# 13. hidden role=switch checkbox ignored
def test_hidden_switch_checkbox_ignored_alongside_real_questions(page):
    page.set_content(
        _questions_page(_ONSITE_RADIOGROUP_FIXTURE + _SALARY_TEXTAREA_FIXTURE + _HIDDEN_SWITCH_CHECKBOX)
    )
    result = extract_questions(page)
    assert result.status == QuestionExtractionStatus.QUESTIONS_PRESENT
    assert len(result.questions) == 2


# 14. React-Aria generated id not used as durable ID when name exists
def test_generated_id_not_used_as_question_id_when_name_exists(page):
    page.set_content(_questions_page(_ONSITE_RADIOGROUP_FIXTURE + _SALARY_TEXTAREA_FIXTURE))
    result = extract_questions(page)
    ids = {q.question_id for q in result.questions}
    assert "react-aria-generated-1" not in ids
    assert "react-aria-generated-2" not in ids
    assert ids == {"c59c9cd9-8441-4610-8e13-2621ae1669c2", "96824b6c-c489-4500-9dcc-d82847b7b1b3"}


# 15. onsite question -> NEEDS_INPUT (exact live prompt)
def test_onsite_question_classified_needs_input(page):
    page.set_content(_questions_page(_ONSITE_RADIOGROUP_FIXTURE))
    q = extract_questions(page).questions[0]
    assert q.prompt == "Are you able and willing to regularly come into the office to work?"
    assert q.status == QuestionStatus.NEEDS_INPUT


# 16. expected salary question -> NEEDS_INPUT (exact live prompt)
def test_salary_question_classified_needs_input(page):
    page.set_content(_questions_page(_SALARY_TEXTAREA_FIXTURE))
    q = extract_questions(page).questions[0]
    assert q.prompt == "What is your expected rate or salary?"
    assert q.status == QuestionStatus.NEEDS_INPUT


# 17-19: no-questions Review page, Work Authorization, Current Location --
# already covered above by the Phase 4D-A tests in this same file.


# 20. unknown visible control -> UNSUPPORTED, not silently ignored
def test_unsupported_control_classified_explicitly(page):
    page.set_content(_questions_page('<label>Years of experience</label><input type="text" id="yoe">'))
    result = extract_questions(page)
    assert result.status == QuestionExtractionStatus.QUESTIONS_PRESENT
    assert len(result.questions) == 1
    q = result.questions[0]
    assert q.field_type == FieldType.UNSUPPORTED
    assert q.status == QuestionStatus.UNSUPPORTED


# 21. already-answered supported question -> ALREADY_ANSWERED
_FILLED_SALARY_TEXTAREA_FIXTURE = """
<div>
  <p id="ta-label2">What is your expected rate or salary?</p>
  <textarea id="react-aria-generated-3" aria-labelledby="ta-label2" name="filled-question-uuid">50000</textarea>
</div>
"""

_ANSWERED_RADIOGROUP_FIXTURE = """
<div>
  <p id="rg-label2">Some other yes/no question?</p>
  <div role="radiogroup" aria-labelledby="rg-label2">
    <label><input type="radio" name="answered-radio-uuid" value="yes" checked> Yes</label>
    <label><input type="radio" name="answered-radio-uuid" value="no"> No</label>
  </div>
</div>
"""


def test_already_answered_textarea_status(page):
    page.set_content(_questions_page(_FILLED_SALARY_TEXTAREA_FIXTURE))
    q = extract_questions(page).questions[0]
    assert q.current_value == "50000"
    assert q.status == QuestionStatus.ALREADY_ANSWERED


def test_already_answered_radiogroup_status(page):
    page.set_content(_questions_page(_ANSWERED_RADIOGROUP_FIXTURE))
    q = extract_questions(page).questions[0]
    assert q.current_value == "Yes"
    assert q.status == QuestionStatus.ALREADY_ANSWERED


# 22. duplicate-looking prompts remain structurally distinct by question identifier
_DUPLICATE_PROMPT_RADIOGROUP_A = """
<div>
  <p id="dup-label-a">Are you willing to work overtime?</p>
  <div role="radiogroup" aria-labelledby="dup-label-a">
    <label><input type="radio" name="dup-uuid-a" value="yes"> Yes</label>
    <label><input type="radio" name="dup-uuid-a" value="no"> No</label>
  </div>
</div>
"""

_DUPLICATE_PROMPT_RADIOGROUP_B = """
<div>
  <p id="dup-label-b">Are you willing to work overtime?</p>
  <div role="radiogroup" aria-labelledby="dup-label-b">
    <label><input type="radio" name="dup-uuid-b" value="yes"> Yes</label>
    <label><input type="radio" name="dup-uuid-b" value="no"> No</label>
  </div>
</div>
"""


def test_duplicate_prompts_remain_structurally_distinct(page):
    page.set_content(_questions_page(_DUPLICATE_PROMPT_RADIOGROUP_A + _DUPLICATE_PROMPT_RADIOGROUP_B))
    result = extract_questions(page)
    assert len(result.questions) == 2
    ids = {q.question_id for q in result.questions}
    assert ids == {"dup-uuid-a", "dup-uuid-b"}
    assert all(q.prompt == "Are you willing to work overtime?" for q in result.questions)
