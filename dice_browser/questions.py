"""Phase 4D: question extraction/classification only. No answering, no
filling, no selecting, no Next/Continue/Back/Review/Submit -- those don't
exist anywhere in this module, and this phase doesn't build them.

Two real live branches observed and locked in (2026-08-21):

1. NO_QUESTIONS_PRESENT -- Data Engineer @ Stefanini and other 2-step
   jobs (QUANTUM TECHNOLOGIES, MSYS x2): Step 2 is a read-only "Review
   your application" screen with zero fillable controls. Work
   Authorization and Current Location are pulled from the candidate's
   existing profile and shown for confirmation, not asked fresh.

2. QUESTIONS_PRESENT -- Java Developer @ Yashnee Tech Solutions
   (job 3f63223a-1dc9-4af9-914c-4ed01e625d44, a 3-step wizard): Step 2 is
   a dedicated "Application Questions" screen with two real controls --
   a role="radiogroup" (Yes/No) and a <textarea> -- neither exposing a
   required/aria-required attribute or visible required marker anywhere,
   which is why RequiredState exists as a tri-state rather than a bool.

is_review_screen() was generalized from a literal "Step 2 of 2" (the only
shape seen when this was written) to any "Step X of Y" indicator, since
Yashnee's wizard proved step counts vary (3 steps, not 2) -- a defensive
correction, not itself independently live-verified for a 3rd step.
"""
from __future__ import annotations

import hashlib
import re

from playwright.sync_api import Page

from dice_browser.models import (
    FieldType,
    QuestionExtractionResult,
    QuestionExtractionStatus,
    QuestionField,
    QuestionStatus,
    RequiredState,
)

_REVIEW_HEADING_TEXT = "Review your application"
_QUESTIONS_HEADING_TEXT = "Application Questions"
_STEP_INDICATOR_PATTERN = re.compile(r"Step \d+ of \d+")
_COUNTER_PATTERN = re.compile(r"^\d+\s*/\s*\d+$")

_CANDIDATE_CONTROL_SELECTOR = "input, select, textarea, [role='radio'], [role='checkbox'], [role='combobox']"


def is_review_screen(page: Page) -> bool:
    """Explicit, deterministic Review-screen detection: a step indicator,
    the review heading, and a Submit button must ALL be present. No
    single signal is trusted alone."""
    body_text = page.inner_text("body")
    has_step = bool(_STEP_INDICATOR_PATTERN.search(body_text))
    has_heading = _REVIEW_HEADING_TEXT in body_text
    has_submit = page.get_by_role("button", name="Submit", exact=False).count() > 0
    return has_step and has_heading and has_submit


def is_questions_screen(page: Page) -> bool:
    """Explicit, deterministic Application-Questions-screen detection:
    live-verified shape (2026-08-21) is a step indicator plus the
    "Application Questions" heading.

    Real live regression (2026-08-21, Yashnee Tech Solutions job
    3f63223a-1dc9-4af9-914c-4ed01e625d44, Step 3 of 3): the Review screen
    itself shows a "Application Questions * / Completed" summary line for
    the already-finished step, which the heading-text check alone would
    misread as an active questions step. Review detection wins over any
    incidental summary text it happens to contain -- never page-wide text
    alone."""
    if is_review_screen(page):
        return False
    body_text = page.inner_text("body")
    has_step = bool(_STEP_INDICATOR_PATTERN.search(body_text))
    has_heading = _QUESTIONS_HEADING_TEXT in body_text
    return has_step and has_heading


def _find_candidate_controls(page: Page):
    """Visible input/select/textarea/ARIA-widget elements only. Never
    treats a hidden control as a question -- the real live page's
    recurring hidden `role="switch"` checkbox (seen on every Dice wizard
    page observed so far) must never surface as a screening question."""
    return [el for el in page.locator(_CANDIDATE_CONTROL_SELECTOR).all() if el.is_visible()]


def _resolve_by_id(page: Page, element_id: str | None) -> str | None:
    if not element_id:
        return None
    loc = page.locator(f"#{element_id}")
    if loc.count() != 1:
        return None
    try:
        text = loc.first.inner_text().strip()
    except Exception:
        return None
    return text or None


def _find_question_container(page: Page, control, labelledby_id: str | None):
    """Smallest ancestor <div> of `control` that also contains the prompt
    element -- used to scope adjacent-helper-text lookup. Bounded to 3
    ancestor levels; never a page-wide search."""
    if not labelledby_id:
        return None
    prompt_loc = page.locator(f"#{labelledby_id}")
    if prompt_loc.count() != 1:
        return None
    prompt_handle = prompt_loc.first.element_handle()
    if prompt_handle is None:
        return None
    for level in (1, 2, 3):
        container = control.locator(f"xpath=ancestor::div[{level}]")
        if container.count() != 1:
            continue
        try:
            contains = container.first.evaluate("(el, promptEl) => el.contains(promptEl)", prompt_handle)
        except Exception:
            contains = False
        if contains:
            return container.first
    return None


def _resolve_adjacent_helper(page: Page, control, labelledby_id: str | None, prompt_text: str | None) -> str | None:
    """Fallback helper-text resolution when no aria-describedby link
    exists (live-verified: Yashnee's textarea helper text is visually
    adjacent but not ARIA-linked). Scoped strictly to the question's own
    container -- never a page-wide text search -- and excludes the prompt
    itself and the "N/NNNN" character-counter text."""
    container = _find_question_container(page, control, labelledby_id)
    if container is None:
        return None
    for cand in container.locator("p, span").all():
        try:
            txt = cand.inner_text().strip()
        except Exception:
            continue
        if not txt or txt == prompt_text or _COUNTER_PATTERN.match(txt):
            continue
        return txt
    return None


def _question_id(name_attr: str | None, prompt: str | None, index: int) -> str:
    """Prefer the DOM `name` attribute -- live-verified to be a stable,
    UUID-shaped Dice question identifier. Never the React-Aria generated
    `id` (live-verified to be per-render, not durable). Falls back to a
    hash of the resolved prompt text (still content-stable across
    reloads), and only as a last resort an explicitly-unstable positional
    placeholder."""
    if name_attr:
        return name_attr
    if prompt:
        return f"prompt-hash-{hashlib.sha256(prompt.encode()).hexdigest()[:16]}"
    return f"unstable-{index}"


def _radio_option_label(radio) -> str | None:
    try:
        return radio.evaluate("e => { const l = e.closest('label'); return l ? l.innerText.trim() : null; }")
    except Exception:
        return None


def _extract_radiogroup(page: Page, radios: list, index: int) -> QuestionField:
    group = None
    try:
        candidate_group = radios[0].locator("xpath=ancestor::*[@role='radiogroup'][1]")
        if candidate_group.count() == 1:
            group = candidate_group
    except Exception:
        group = None

    labelledby = group.get_attribute("aria-labelledby") if group is not None else None
    describedby = group.get_attribute("aria-describedby") if group is not None else None
    prompt = _resolve_by_id(page, labelledby)
    helper = _resolve_by_id(page, describedby)

    name_attr = radios[0].get_attribute("name")
    options: list[str] = []
    current_value = None
    for radio in radios:
        label = _radio_option_label(radio)
        if label:
            options.append(label)
        if label and radio.is_checked():
            current_value = label

    status = QuestionStatus.ALREADY_ANSWERED if current_value else QuestionStatus.NEEDS_INPUT

    return QuestionField(
        question_id=_question_id(name_attr, prompt, index),
        prompt=prompt,
        field_type=FieldType.RADIO,
        required_state=RequiredState.UNKNOWN,
        options=tuple(options) if options else None,
        current_value=current_value,
        helper=helper,
        status=status,
    )


def _extract_textarea(page: Page, control, index: int) -> QuestionField:
    labelledby = control.get_attribute("aria-labelledby")
    describedby = control.get_attribute("aria-describedby")
    prompt = _resolve_by_id(page, labelledby)
    helper = (
        _resolve_by_id(page, describedby)
        if describedby
        else _resolve_adjacent_helper(page, control, labelledby, prompt)
    )

    name_attr = control.get_attribute("name")
    try:
        raw_value = control.input_value()
    except Exception:
        raw_value = None
    current_value = raw_value if raw_value else None

    status = QuestionStatus.ALREADY_ANSWERED if current_value else QuestionStatus.NEEDS_INPUT

    return QuestionField(
        question_id=_question_id(name_attr, prompt, index),
        prompt=prompt,
        field_type=FieldType.TEXTAREA,
        required_state=RequiredState.UNKNOWN,
        options=None,
        current_value=current_value,
        helper=helper,
        status=status,
    )


def _unsupported_question(index: int) -> QuestionField:
    return QuestionField(
        question_id=f"unclassified-{index}",
        prompt=None,
        field_type=FieldType.UNSUPPORTED,
        required_state=RequiredState.UNKNOWN,
        options=None,
        current_value=None,
        helper=None,
        status=QuestionStatus.UNSUPPORTED,
    )


def extract_questions(page: Page) -> QuestionExtractionResult:
    """Detection/extraction/classification only -- never clicks Next,
    Back, Review, or Submit, never fills or selects anything. Refuses to
    guess "no questions" on an unrecognized page (UNKNOWN_SCREEN).

    Radio inputs sharing a `name` are grouped into one RADIO question
    (live-verified: Dice groups Yes/No options this way, not via
    <fieldset>). Every other visible candidate control (select, native
    checkbox, custom combobox, plain text input, etc.) is UNSUPPORTED --
    never guessed into RADIO/TEXTAREA, and never silently dropped."""
    if not (is_review_screen(page) or is_questions_screen(page)):
        return QuestionExtractionResult(status=QuestionExtractionStatus.UNKNOWN_SCREEN, questions=())

    candidates = _find_candidate_controls(page)
    if not candidates:
        return QuestionExtractionResult(status=QuestionExtractionStatus.NO_QUESTIONS_PRESENT, questions=())

    questions: list[QuestionField] = []
    seen_radio_names: set[str] = set()
    for index, control in enumerate(candidates):
        tag = control.evaluate("e => e.tagName")
        ctype = control.get_attribute("type")

        if tag == "INPUT" and ctype == "radio":
            name_attr = control.get_attribute("name")
            key = name_attr or f"__unnamed_radio_{index}"
            if key in seen_radio_names:
                continue
            seen_radio_names.add(key)
            group_radios = [
                c
                for c in candidates
                if c.evaluate("e => e.tagName") == "INPUT"
                and c.get_attribute("type") == "radio"
                and (c.get_attribute("name") or f"__unnamed_radio_{index}") == key
            ]
            questions.append(_extract_radiogroup(page, group_radios, index))
        elif tag == "TEXTAREA":
            questions.append(_extract_textarea(page, control, index))
        else:
            questions.append(_unsupported_question(index))

    return QuestionExtractionResult(status=QuestionExtractionStatus.QUESTIONS_PRESENT, questions=tuple(questions))
