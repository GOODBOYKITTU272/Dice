"""Phase 6: dice/answer_resolution.py. The safe-mapping table is
deliberately empty as of Phase 6 -- these tests lock in that both real
live-observed questions (on-site willingness, expected salary) correctly
resolve to None (never guessed), and that the function structurally
refuses to ever resolve a sensitive field even if a future mapping tried
to route one through it.
"""
from __future__ import annotations

from dice.answer_resolution import resolve_safe_answer
from dice.models import CandidateProfile
from dice_browser.models import FieldType, QuestionField, QuestionStatus, RequiredState


def _candidate(**overrides) -> CandidateProfile:
    base = dict(
        candidate_id="cand-1",
        name="Jordan Rivera",
        email="jordan@example.com",
        phone="+1-555-0100",
        location=None,
        visa_type="H1B",
        work_authorized=True,
        requires_sponsorship=False,
        willing_to_relocate=True,
        experience_years=6,
        desired_start_date="2026-09-01",
        resume_url="https://example.com/resume.pdf",
        linkedin_url="https://linkedin.com/in/jordanrivera",
        github_url="https://github.com/jordanrivera",
    )
    base.update(overrides)
    return CandidateProfile(**base)


def _radio_question(prompt: str) -> QuestionField:
    return QuestionField(
        question_id="q-1", prompt=prompt, field_type=FieldType.RADIO,
        required_state=RequiredState.UNKNOWN, options=("Yes", "No"),
        current_value=None, helper=None, status=QuestionStatus.NEEDS_INPUT,
    )


def _textarea_question(prompt: str) -> QuestionField:
    return QuestionField(
        question_id="q-2", prompt=prompt, field_type=FieldType.TEXTAREA,
        required_state=RequiredState.UNKNOWN, options=None,
        current_value=None, helper=None, status=QuestionStatus.NEEDS_INPUT,
    )


def test_onsite_question_has_no_safe_mapping():
    q = _radio_question("Are you able and willing to regularly come into the office to work?")
    assert resolve_safe_answer(q, _candidate()) is None


def test_salary_question_has_no_safe_mapping():
    q = _textarea_question("What is your expected rate or salary?")
    assert resolve_safe_answer(q, _candidate()) is None


def test_unmapped_prompt_returns_none():
    q = _textarea_question("Some question that has never been seen live before")
    assert resolve_safe_answer(q, _candidate()) is None


def test_none_prompt_returns_none():
    q = QuestionField(
        question_id="q-3", prompt=None, field_type=FieldType.TEXTAREA,
        required_state=RequiredState.UNKNOWN, options=None,
        current_value=None, helper=None, status=QuestionStatus.NEEDS_INPUT,
    )
    assert resolve_safe_answer(q, _candidate()) is None


def test_unsupported_field_type_returns_none():
    q = QuestionField(
        question_id="q-4", prompt="anything", field_type=FieldType.UNSUPPORTED,
        required_state=RequiredState.UNKNOWN, options=None,
        current_value=None, helper=None, status=QuestionStatus.UNSUPPORTED,
    )
    assert resolve_safe_answer(q, _candidate()) is None


def test_sensitive_field_never_resolved_even_if_mapped(monkeypatch):
    # Defends the invariant even against a hypothetical future mistake:
    # if someone ever added a mapping pointing at a sensitive field, the
    # underlying resolve_candidate_field() accessor still refuses it.
    import dice.answer_resolution as module

    monkeypatch.setitem(module._SAFE_PROMPT_TO_FIELD, "Are you authorized to work without sponsorship?", "work_authorized")
    q = _radio_question("Are you authorized to work without sponsorship?")
    assert resolve_safe_answer(q, _candidate()) is None
