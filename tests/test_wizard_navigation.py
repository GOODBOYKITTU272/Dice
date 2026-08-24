"""Phase 6: wizard_navigation.py -- filling an already-resolved answer,
clicking Next. Offline tests only (synthetic HTML); no live Dice needed.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright

from dice_browser.models import FieldType, QuestionField, QuestionStatus, RequiredState
from dice_browser.wizard_navigation import AnswerFillFailedError, UnsupportedFieldTypeError, click_next, fill_answer


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


def _radio_question(question_id="onsite-question-uuid", options=("Yes", "No")):
    return QuestionField(
        question_id=question_id,
        prompt="Are you able and willing to regularly come into the office to work?",
        field_type=FieldType.RADIO,
        required_state=RequiredState.UNKNOWN,
        options=options,
        current_value=None,
        helper=None,
        status=QuestionStatus.NEEDS_INPUT,
    )


def _textarea_question(question_id="salary-question-uuid"):
    return QuestionField(
        question_id=question_id,
        prompt="What is your expected rate or salary?",
        field_type=FieldType.TEXTAREA,
        required_state=RequiredState.UNKNOWN,
        options=None,
        current_value=None,
        helper=None,
        status=QuestionStatus.NEEDS_INPUT,
    )


def _radio_fixture(question_id="onsite-question-uuid"):
    return f"""
    <html><body>
    <label><input type="radio" name="{question_id}" value="yes"> Yes</label>
    <label><input type="radio" name="{question_id}" value="no"> No</label>
    </body></html>
    """


def _textarea_fixture(question_id="salary-question-uuid"):
    return f"""
    <html><body>
    <textarea name="{question_id}" placeholder="Your answer here..."></textarea>
    </body></html>
    """


# ── fill_answer: RADIO ────────────────────────────────────────────────────


def test_fill_radio_selects_correct_option(page):
    page.set_content(_radio_fixture())
    fill_answer(page, _radio_question(), "Yes")
    checked = page.locator("input[type='radio']:checked")
    assert checked.count() == 1
    label = checked.first.evaluate("e => e.closest('label').innerText.trim()")
    assert label == "Yes"


def test_fill_radio_rejects_answer_not_in_options(page):
    page.set_content(_radio_fixture())
    try:
        fill_answer(page, _radio_question(), "Maybe")
        assert False, "expected AnswerFillFailedError"
    except AnswerFillFailedError:
        pass
    assert page.locator("input[type='radio']:checked").count() == 0


def test_fill_radio_raises_when_no_inputs_found_for_question_id(page):
    page.set_content(_radio_fixture(question_id="a-different-question-uuid"))
    try:
        fill_answer(page, _radio_question(question_id="onsite-question-uuid"), "Yes")
        assert False, "expected AnswerFillFailedError"
    except AnswerFillFailedError:
        pass


def test_fill_radio_works_when_custom_styled_overlay_intercepts_pointer_events(page):
    # Real live finding (2026-08-24, job 3f63223a-1dc9-4af9-914c-4ed01e625d44
    # Step 3 "Are you able and willing to regularly come into the office to
    # work?"): the real radiogroup wraps each input in a custom-styled
    # visual layer (a sibling div drawing the circle/checkmark) that sits
    # on top of the real <input type=radio> and intercepts pointer events.
    # A plain .check() (real simulated click) retried for the full 30s
    # timeout and never actually clicked -- Playwright's own actionability
    # check refuses to click through an intercepting element by design.
    page.set_content(
        f"""
        <html><body>
        <div role="radiogroup" style="position: relative;">
          <label style="position: relative; display: block; width: 100px; height: 20px;">
            <input type="radio" name="{"onsite-question-uuid"}" value="yes"
                   style="position: absolute; inset: 0; opacity: 0;">
            <div style="position: absolute; inset: 0;">Yes</div>
          </label>
          <label style="position: relative; display: block; width: 100px; height: 20px;">
            <input type="radio" name="{"onsite-question-uuid"}" value="no"
                   style="position: absolute; inset: 0; opacity: 0;">
            <div style="position: absolute; inset: 0;">No</div>
          </label>
        </div>
        </body></html>
        """
    )
    fill_answer(page, _radio_question(), "Yes")
    checked = page.locator("input[type='radio']:checked")
    assert checked.count() == 1
    label = checked.first.evaluate("e => e.closest('label').innerText.trim()")
    assert label == "Yes"


def test_fill_radio_clicks_react_aria_pressable_wrapper_when_present(page):
    # Real live finding (2026-08-24), a second real-shape failure past the
    # overlay-interception fix above: the real radiogroup is a React-Aria
    # <Radio> component (marked data-react-aria-pressable="true") whose own
    # gesture handling lives on a wrapper element carrying data-rac, not on
    # the native input directly -- even a force=True click ON THE INPUT
    # dispatched successfully (per Playwright's log) but never toggled
    # `checked`, raising "Clicking the checkbox did not change its state".
    # The nearest data-rac ancestor is the actual real click target
    # (mirrors _extract_select's identical data-rac group-scoping, never a
    # page-wide search).
    page.set_content(
        f"""
        <html><body>
        <div role="radiogroup">
          <label data-rac="" onclick="this.querySelector('input').checked = true;
                                       this.querySelector('input').dispatchEvent(new Event('change', {{bubbles: true}}));">
            <input type="radio" name="{"onsite-question-uuid"}" value="1" data-react-aria-pressable="true"
                   onclick="this.checked = false;">
            Yes
          </label>
          <label data-rac="" onclick="this.querySelector('input').checked = true;
                                       this.querySelector('input').dispatchEvent(new Event('change', {{bubbles: true}}));">
            <input type="radio" name="{"onsite-question-uuid"}" value="2" data-react-aria-pressable="true"
                   onclick="this.checked = false;">
            No
          </label>
        </div>
        </body></html>
        """
    )
    # Adversarial: the input's own click handler actively un-checks itself
    # (simulating the real page, where a direct click on the input never
    # results in a checked state) -- this test only passes if fill_answer
    # actually clicks the data-rac wrapper, never the raw input.
    fill_answer(page, _radio_question(), "Yes")
    checked = page.locator("input[type='radio']:checked")
    assert checked.count() == 1
    label = checked.first.evaluate("e => e.closest('label').innerText.trim()")
    assert label == "Yes"


def test_fill_radio_raises_when_option_label_not_found(page):
    page.set_content(
        """
        <html><body>
        <label><input type="radio" name="onsite-question-uuid" value="maybe"> Maybe</label>
        </body></html>
        """
    )
    q = _radio_question(options=None)  # no recorded options -- skip the pre-check, exercise the label-not-found path
    try:
        fill_answer(page, q, "Yes")
        assert False, "expected AnswerFillFailedError"
    except AnswerFillFailedError:
        pass


# ── fill_answer: TEXTAREA ──────────────────────────────────────────────────


def test_fill_textarea_sets_value(page):
    page.set_content(_textarea_fixture())
    fill_answer(page, _textarea_question(), "50000")
    assert page.locator("textarea[name='salary-question-uuid']").input_value() == "50000"


def test_fill_textarea_raises_when_not_uniquely_found(page):
    page.set_content("<html><body></body></html>")
    try:
        fill_answer(page, _textarea_question(), "50000")
        assert False, "expected AnswerFillFailedError"
    except AnswerFillFailedError:
        pass


# ── fill_answer: SELECT (2026-08-24 live finding) ──────────────────────────


def _select_question(question_id="workAuthorization"):
    return QuestionField(
        question_id=question_id,
        prompt="Work Authorization *",
        field_type=FieldType.SELECT,
        required_state=RequiredState.UNKNOWN,
        options=("US Citizen", "Have H1 Visa", "Green Card Holder"),
        current_value=None,
        helper=None,
        status=QuestionStatus.NEEDS_INPUT,
    )


def _select_fixture(question_id="workAuthorization"):
    return f"""
    <html><body>
    <select name="{question_id}">
      <option value="">&nbsp;</option>
      <option value="US_CITIZEN">US Citizen</option>
      <option value="HAVE_H1_VISA">Have H1 Visa</option>
      <option value="GREEN_CARD_HOLDER">Green Card Holder</option>
    </select>
    </body></html>
    """


def test_fill_select_chooses_correct_option(page):
    page.set_content(_select_fixture())
    fill_answer(page, _select_question(), "Have H1 Visa")
    assert page.locator("select[name='workAuthorization']").input_value() == "HAVE_H1_VISA"


def test_fill_select_rejects_answer_not_in_options(page):
    page.set_content(_select_fixture())
    try:
        fill_answer(page, _select_question(), "Canadian Citizen")
        assert False, "expected AnswerFillFailedError"
    except AnswerFillFailedError:
        pass
    assert page.locator("select[name='workAuthorization']").input_value() == ""


def test_fill_select_raises_when_not_uniquely_found(page):
    page.set_content("<html><body></body></html>")
    try:
        fill_answer(page, _select_question(), "Have H1 Visa")
        assert False, "expected AnswerFillFailedError"
    except AnswerFillFailedError:
        pass


# ── fill_answer: TEXT_INPUT (2026-08-24 live finding) ───────────────────────


def _text_input_question(question_id="candidateLocation"):
    return QuestionField(
        question_id=question_id,
        prompt="What is your current city of residence? *",
        field_type=FieldType.TEXT_INPUT,
        required_state=RequiredState.UNKNOWN,
        options=None,
        current_value=None,
        helper=None,
        status=QuestionStatus.NEEDS_INPUT,
    )


def _text_input_fixture(question_id="candidateLocation"):
    return f"""
    <html><body>
    <input type="text" name="{question_id}" placeholder="Enter your city or postal code">
    </body></html>
    """


def test_fill_text_input_sets_value(page):
    page.set_content(_text_input_fixture())
    fill_answer(page, _text_input_question(), "West Haven, CT")
    assert page.locator("input[name='candidateLocation']").input_value() == "West Haven, CT"


def test_fill_text_input_raises_when_not_uniquely_found(page):
    page.set_content("<html><body></body></html>")
    try:
        fill_answer(page, _text_input_question(), "West Haven, CT")
        assert False, "expected AnswerFillFailedError"
    except AnswerFillFailedError:
        pass


# ── fill_answer: unsupported field type ────────────────────────────────────


def test_fill_answer_raises_for_unsupported_field_type(page):
    page.set_content(_radio_fixture())
    q = QuestionField(
        question_id="x", prompt=None, field_type=FieldType.UNSUPPORTED,
        required_state=RequiredState.UNKNOWN, options=None, current_value=None,
        helper=None, status=QuestionStatus.UNSUPPORTED,
    )
    try:
        fill_answer(page, q, "anything")
        assert False, "expected UnsupportedFieldTypeError"
    except UnsupportedFieldTypeError:
        pass


# ── click_next ──────────────────────────────────────────────────────────


def test_click_next_clicks_when_safely_identifiable(page):
    page.set_content('<html><body><button onclick="window.__clicked=true;">Next</button></body></html>')
    assert click_next(page) is True
    assert page.evaluate("window.__clicked") is True


def test_click_next_returns_false_when_not_present(page):
    page.set_content("<html><body><button>Back</button></body></html>")
    assert click_next(page) is False


def test_click_next_returns_false_when_disabled(page):
    page.set_content('<html><body><button disabled onclick="window.__clicked=true;">Next</button></body></html>')
    assert click_next(page) is False
    assert page.evaluate("window.__clicked") is not True


def test_click_next_waits_for_spa_transition_to_settle(page):
    # Real bug found live in Phase 6 (2026-08-21): Dice's wizard advances
    # via a client-side SPA transition (an in-flight API call renders the
    # next step), not a full page navigation -- the same class of issue
    # easy_apply.py already had to handle for the initial Easy Apply click
    # (see its _poll_for_wizard_opened / the comment on why
    # wait_for_load_state("domcontentloaded") alone isn't reliable here).
    # click_next() returning immediately after the raw .click() let a
    # caller (worker.py's question-walk loop) inspect the screen before
    # the next step's content had rendered, misclassifying a real
    # Review/question screen as UNKNOWN_SCREEN. Measures wall-clock time
    # to prove click_next() actually waits for a slow in-flight request
    # to settle rather than returning as soon as the click dispatches --
    # checking a JS flag afterward wouldn't prove this, since the page's
    # own JS event loop keeps running regardless of when the Python call
    # returns.
    def _slow_route(route):
        import time

        time.sleep(0.4)
        route.fulfill(status=200, body="{}", content_type="application/json")

    page.route("**/slow-transition", _slow_route)
    page.set_content('<html><body><button onclick="fetch(\'/slow-transition\');">Next</button></body></html>')

    import time

    start = time.monotonic()
    assert click_next(page) is True
    elapsed = time.monotonic() - start
    assert elapsed >= 0.35, f"click_next() returned in {elapsed:.3f}s -- did not wait for the in-flight request to settle"


def test_click_next_returns_false_when_ambiguous(page):
    page.set_content(
        """
        <html><body>
        <button onclick="window.__clicked=true;">Next</button>
        <button onclick="window.__clicked=true;">Next Step</button>
        </body></html>
        """
    )
    assert click_next(page) is False
    assert page.evaluate("window.__clicked") is not True
