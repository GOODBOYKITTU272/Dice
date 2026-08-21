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
