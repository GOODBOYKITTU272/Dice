"""Phase 6: safe, narrow auto-answer resolution. Maps a question to a
trusted CandidateProfile value ONLY for prompts with an established,
exact, live-verified safe mapping.

Deliberately empty as of Phase 6's creation (2026-08-21) -- neither of
the two real questions observed live (Java Developer @ Yashnee Tech
Solutions, "Are you able and willing to regularly come into the office
to work?" and "What is your expected rate or salary?") has a trusted
mapping: Phase 4D's own explicit policy is that on-site willingness is
NOT the same concept as willing_to_relocate, and CandidateProfile has no
compensation field at all. Never guesses; returns None whenever there's
no exact, pre-approved mapping, which routes the question to a Phase 4F
intervention (NEEDS_INPUT) instead.
"""
from __future__ import annotations

from dice.candidate_adapter import resolve_candidate_field
from dice.models import CandidateProfile
from dice_browser.models import FieldType, QuestionField

# Extend only with a live-verified prompt string (verbatim, exact match)
# and a CandidateProfile field name that's genuinely safe to disclose
# automatically. Never add visa_type/work_authorized/requires_sponsorship
# here -- those stay permanently excluded by
# candidate_adapter.resolve_candidate_field() regardless of what's in
# this map.
_SAFE_PROMPT_TO_FIELD: dict[str, str] = {}


def resolve_safe_answer(question: QuestionField, candidate: CandidateProfile) -> str | None:
    """Returns a trusted, exact answer for `question`, or None if no
    pre-approved mapping exists -- never an inferred or guessed value."""
    if question.field_type not in (FieldType.RADIO, FieldType.TEXTAREA):
        return None
    if not question.prompt:
        return None

    field_name = _SAFE_PROMPT_TO_FIELD.get(question.prompt.strip())
    if field_name is None:
        return None

    value = resolve_candidate_field(candidate, field_name)
    if value is None:
        return None
    return str(value)
