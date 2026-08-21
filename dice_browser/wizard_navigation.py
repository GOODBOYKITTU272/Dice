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
    live-verified stable identifier from Phase 4D). Only RADIO and
    TEXTAREA are supported; anything else raises rather than guessing at
    a fill strategy for a control type this codebase has no live
    evidence for."""
    if question.field_type == FieldType.RADIO:
        _fill_radio(page, question, answer)
    elif question.field_type == FieldType.TEXTAREA:
        _fill_textarea(page, question, answer)
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

    target.check()


def _fill_textarea(page: Page, question: QuestionField, answer: str) -> None:
    textarea = page.locator(f"textarea[name='{question.question_id}']")
    if textarea.count() != 1:
        raise AnswerFillFailedError(f"textarea for question_id={question.question_id!r} not uniquely found")
    textarea.first.fill(answer)


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
