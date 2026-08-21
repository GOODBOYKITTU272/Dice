"""Deterministic C2C (Corp-to-Corp) classifier. No LLM, no guessing.

Per the TRD: "Contract or Third Party is a search funnel, not proof of
C2C. Classification must inspect explicit description evidence." So the
Dice-level Contract/Third-Party filter (dice/search.py) only narrows what
we look at — this module is what actually decides CONFIRMED / LIKELY /
NOT_C2C / UNKNOWN, from the job description text plus the employment-type
text as a structural fallback signal.

Rules (explicit, not a loose keyword dump):
  1. Negative phrases are checked first and are literal, specific phrases
     (not "any negation of a positive phrase") — exactly the phrases V1
     asked for: "no c2c", "no corp to corp", "w2 only", "no third parties"
     (and the "party"/"parties" variant), "no vendors".
  2. Positive phrases are similarly literal and specific: "corp to corp",
     "corp-to-corp", "c2c" (word-boundaried so it can't match inside
     another word), "third party candidates", "third party vendors",
     "subcontractor", "contract corp".
  3. If both fire: negative overrides positive (V1 decision — err toward
     NOT_C2C rather than a false CONFIRMED, consistent with this project's
     general "don't guess, don't assume the generous case" posture).
  4. If only negative fires: NOT_C2C.
  5. If only positive fires: CONFIRMED (these are explicit phrases, not
     generic contract language — the PRD calls these "strong positive
     evidence").
  6. If neither fires in the description, but the job's employment-type
     text (from Dice's own categorization) says "Third Party": LIKELY —
     structural signal without an explicit phrase, matching the TRD's
     LIKELY definition.
  7. Otherwise: UNKNOWN — insufficient evidence.
"""
from __future__ import annotations

import re

from dice.models import C2CResult

_NEGATIVE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("no c2c", re.compile(r"\bno\s+c2c\b", re.IGNORECASE)),
    ("no corp to corp", re.compile(r"\bno\s+corp[\s-]?to[\s-]?corp\b", re.IGNORECASE)),
    ("w2 only", re.compile(r"\bw[\s-]?2\s+only\b", re.IGNORECASE)),
    ("no third parties", re.compile(r"\bno\s+third[\s-]?part(?:y|ies)\b", re.IGNORECASE)),
    ("no vendors", re.compile(r"\bno\s+vendors?\b", re.IGNORECASE)),
]

_POSITIVE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("corp to corp", re.compile(r"\bcorp[\s-]?to[\s-]?corp\b", re.IGNORECASE)),
    ("c2c", re.compile(r"\bc2c\b", re.IGNORECASE)),
    ("third party candidates", re.compile(r"\bthird[\s-]?part(?:y|ies)\s+candidates?\b", re.IGNORECASE)),
    ("third party vendors", re.compile(r"\bthird[\s-]?part(?:y|ies)\s+vendors?\b", re.IGNORECASE)),
    ("subcontractor", re.compile(r"\bsubcontractors?\b", re.IGNORECASE)),
    ("contract corp", re.compile(r"\bcontract\s+corp\b", re.IGNORECASE)),
]


def _find_matches(text: str, patterns: list[tuple[str, re.Pattern[str]]]) -> list[str]:
    matches = []
    for label, pattern in patterns:
        if pattern.search(text):
            matches.append(label)
    return matches


def classify_c2c(description: str, employment_type_text: str = "") -> C2CResult:
    description = description or ""
    employment_type_text = employment_type_text or ""

    negative_matches = _find_matches(description, _NEGATIVE_PATTERNS)
    positive_matches = _find_matches(description, _POSITIVE_PATTERNS)

    if negative_matches and positive_matches:
        return C2CResult(
            status="NOT_C2C",
            reason=(
                f"Negative evidence overrides conflicting positive terms: "
                f"negative={negative_matches}, positive={positive_matches}"
            ),
            evidence=negative_matches + positive_matches,
        )

    if negative_matches:
        return C2CResult(
            status="NOT_C2C",
            reason=f"Explicit negative evidence: {negative_matches}",
            evidence=negative_matches,
        )

    if positive_matches:
        return C2CResult(
            status="CONFIRMED",
            reason=f"Explicit positive evidence: {positive_matches}",
            evidence=positive_matches,
        )

    if "third party" in employment_type_text.lower():
        return C2CResult(
            status="LIKELY",
            reason=(
                "No explicit C2C phrase in description, but Dice's own "
                f"employment-type categorization says {employment_type_text!r}"
            ),
            evidence=[f"employment_type={employment_type_text}"],
        )

    return C2CResult(
        status="UNKNOWN",
        reason="No C2C evidence found in description or employment type",
        evidence=[],
    )
