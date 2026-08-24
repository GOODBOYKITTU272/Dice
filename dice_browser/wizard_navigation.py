"""Phase 6: safe, narrow wizard progression -- filling an ALREADY-RESOLVED
answer into a RADIO/TEXTAREA question, and clicking Next. This module
never invents an answer (that decision is made upstream, by Phase 4D's
extraction/classification plus Phase 6's own answer_resolution.py or a
resolved Supabase intervention) and never clicks Submit/Review -- that
stays exclusively in dice_browser/submission.py, locked in by a boundary
test just like this module's own click-Next-only scope is.
"""
from __future__ import annotations

from playwright.sync_api import Page

from dice_browser.models import FieldType, QuestionField


class UnsupportedFieldTypeError(ValueError):
    """Raised when asked to fill a field_type this module doesn't know
    how to fill (currently: anything but RADIO/TEXTAREA)."""


class AnswerFillFailedError(RuntimeError):
    """Raised when the live control matching a question can't be found
    or the supplied answer doesn't match its recorded options."""


def fill_answer(page: Page, question: QuestionField, answer: str) -> None:
    """Fills exactly one already-resolved answer into its matching live
    control, scoped by question_id (the DOM `name` attribute -- the
    live-verified stable identifier from Phase 4D). RADIO, TEXTAREA,
    SELECT, and TEXT_INPUT are supported; anything else raises rather
    than guessing at a fill strategy for a control type this codebase
    has no live evidence for."""
    if question.field_type == FieldType.RADIO:
        _fill_radio(page, question, answer)
    elif question.field_type == FieldType.TEXTAREA:
        _fill_textarea(page, question, answer)
    elif question.field_type == FieldType.SELECT:
        _fill_select(page, question, answer)
    elif question.field_type == FieldType.TEXT_INPUT:
        _fill_text_input(page, question, answer)
    else:
        raise UnsupportedFieldTypeError(f"cannot fill field_type={question.field_type!r}")


def _fill_radio(page: Page, question: QuestionField, answer: str) -> None:
    if question.options and answer not in question.options:
        raise AnswerFillFailedError(f"{answer!r} is not one of the recorded options {question.options!r}")

    radios = page.locator(f"input[type='radio'][name='{question.question_id}']").all()
    if not radios:
        raise AnswerFillFailedError(f"no radio inputs found for question_id={question.question_id!r}")

    target = None
    for radio in radios:
        try:
            label = radio.evaluate("e => { const l = e.closest('label'); return l ? l.innerText.trim() : null; }")
        except Exception:
            label = None
        if label == answer:
            target = radio
            break

    if target is None:
        raise AnswerFillFailedError(f"no radio option labeled {answer!r} found for question_id={question.question_id!r}")

    # Real live finding (2026-08-24, job 3f63223a-1dc9-4af9-914c-4ed01e625d44
    # Step 3 onsite question), two layers deep:
    # 1. The real radiogroup draws a custom visual circle/checkmark layer
    #    over the actual <input type=radio> -- a plain .check() (real
    #    simulated pointer click) retried Playwright's own actionability
    #    check for the full default timeout and never clicked, since that
    #    check refuses to click through an intercepting element.
    # 2. force=True alone still isn't enough: the real control is a
    #    React-Aria <Radio> (data-react-aria-pressable="true") whose own
    #    gesture handling lives on its wrapper element (data-rac), not the
    #    native input -- a forced click landed on the input and Playwright
    #    reported the click as delivered, but the input's checked state
    #    never actually changed ("Clicking the checkbox did not change its
    #    state"), live-verified twice against the real wizard.
    # The nearest data-rac ancestor is the real, correct click target --
    # mirrors _extract_select's identical data-rac group-scoping, never a
    # page-wide search -- live-verified to toggle both the underlying
    # native input AND the visible custom UI correctly and exclusively.
    # Falls back to a direct forced check for any simpler/older Dice UI
    # variant that has no such wrapper.
    wrapper = target.locator("xpath=ancestor::*[@data-rac][1]")
    if wrapper.count() == 1:
        wrapper.first.click()
    else:
        target.check(force=True)


def _fill_textarea(page: Page, question: QuestionField, answer: str) -> None:
    textarea = page.locator(f"textarea[name='{question.question_id}']")
    if textarea.count() != 1:
        raise AnswerFillFailedError(f"textarea for question_id={question.question_id!r} not uniquely found")
    textarea.first.fill(answer)


def _fill_select(page: Page, question: QuestionField, answer: str) -> None:
    """Real live shape (2026-08-24): the native <select> is visually
    hidden behind a custom listbox button, but Playwright's
    select_option() operates on the underlying DOM element directly and
    dispatches a real change event -- the same event the custom UI's own
    React state listens for to update its visible display, so this
    updates both the real form value and what the human-facing button
    shows without ever touching the custom button/listbox UI itself."""
    if question.options and answer not in question.options:
        raise AnswerFillFailedError(f"{answer!r} is not one of the recorded options {question.options!r}")

    select = page.locator(f"select[name='{question.question_id}']")
    if select.count() != 1:
        raise AnswerFillFailedError(f"select for question_id={question.question_id!r} not uniquely found")

    try:
        select.first.select_option(label=answer)
    except Exception as exc:
        raise AnswerFillFailedError(f"no option labeled {answer!r} found for question_id={question.question_id!r}") from exc


def _fill_text_input(page: Page, question: QuestionField, answer: str) -> None:
    inp = page.locator(f"input[name='{question.question_id}']")
    if inp.count() != 1:
        raise AnswerFillFailedError(f"input for question_id={question.question_id!r} not uniquely found")
    inp.first.fill(answer)


def click_next(page: Page) -> bool:
    """Clicks the Next button exactly once, only if it's the single,
    visible, enabled Next control on the page. Never clicks
    Submit/Review/Continue -- Next only. Returns False (never clicks)
    when Next isn't safely identifiable, e.g. because the wizard has no
    further step and Review/Submit is showing instead."""
    next_button = page.get_by_role("button", name="Next", exact=False)
    if next_button.count() != 1:
        return False
    if not next_button.first.is_visible() or next_button.first.get_attribute("disabled") is not None:
        return False
    next_button.first.click()
    # Dice's wizard advances via a client-side SPA transition, not a full
    # navigation -- same class of issue easy_apply.py already handles for
    # the initial Easy Apply click. Best-effort settle so a caller
    # inspecting the screen right after doesn't see a mid-transition DOM.
    try:
        page.wait_for_load_state("networkidle", timeout=5_000)
    except Exception:
        pass
    return True
