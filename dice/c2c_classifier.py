"""Deterministic C2C (Corp-to-Corp) classifier. No LLM, no guessing.

Per the TRD: "Contract or Third Party is a search funnel, not proof of
C2C. Classification must inspect explicit description evidence." So the
Dice-level Contract/Third-Party filter (dice/search.py) only narrows what
we look at — this module is what actually decides CONFIRMED / LIKELY /
NOT_C2C / UNKNOWN, from the job description text plus the employment-type
text as a structural fallback signal.

Rules (explicit, not a loose keyword dump):
  1. Negative phrases are checked first. Phase 3C broadened this from a
     handful of literal "no X" phrases to a bounded set of refusal-verb
     *frames*, because real Dice postings phrase refusal many ways ("not
     accepting C2C", "No 3rd Party Subcontractors Permitted", "C2C not
     allowed", "cannot accept C2C"). Each frame is still an explicit,
     reviewable regex anchored on a specific refusal verb (accept/allow/
     permit — never "require"/"need"), not a generic "negation word within
     N characters of C2C" proximity rule. That distinction is deliberate:
     "we do not require previous C2C experience" or "no prior C2C
     experience required" must NOT classify NOT_C2C, since they refuse an
     experience requirement, not the C2C arrangement itself — none of the
     frames below match "require"/"need", so those sentences correctly
     fall through untouched.
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

# Shared target alternation used by the Phase 3C refusal frames below —
# every C2C-adjacent noun a refusal can be phrased about. Deliberately
# excludes generic words like "experience" or "requirement" so a frame can
# never fire on an experience-requirement sentence no matter how it reads.
_C2C_TARGET = r"c2c|corp[\s-]?to[\s-]?corp"
# "3\s?rd" (not just "3rd") because live Dice HTML sometimes marks the
# ordinal suffix as <sup>rd</sup> — real job 173695bb-b7db-427e-b1a9-
# 7b7e8ba0cd20 renders as "3<sup>rd</sup> Party", which the Phase 3C
# whitespace-boundary fix (dice/upstream_adapter.py) correctly turns into
# "3 rd Party" (a real tag boundary), not "3rdParty".
_THIRD_PARTY_TARGET = r"third[\s-]?part(?:y|ies)|3\s?rd[\s-]?part(?:y|ies)"
_SUBCONTRACTOR_TARGET = r"subcontractors?"
_VENDOR_TARGET = r"vendors?"
_ANY_TARGET = f"(?:{_C2C_TARGET}|{_THIRD_PARTY_TARGET}|{_SUBCONTRACTOR_TARGET}|{_VENDOR_TARGET})"

_NEGATIVE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("no c2c", re.compile(r"\bno\s+c2c\b", re.IGNORECASE)),
    ("no corp to corp", re.compile(r"\bno\s+corp[\s-]?to[\s-]?corp\b", re.IGNORECASE)),
    ("w2 only", re.compile(r"\bw[\s-]?2\s+only\b", re.IGNORECASE)),
    ("no third parties", re.compile(r"\bno\s+third[\s-]?part(?:y|ies)\b", re.IGNORECASE)),
    ("no vendors", re.compile(r"\bno\s+vendors?\b", re.IGNORECASE)),
    # Phase 3C additions — bounded refusal-verb frames (see module
    # docstring). Each is anchored on accept/allow/permit, never on
    # require/need, so an experience-requirement sentence can't match.
    ("no 3rd party", re.compile(rf"\bno\s+(?:{_THIRD_PARTY_TARGET})\b", re.IGNORECASE)),
    ("no subcontractors", re.compile(rf"\bno\s+(?:{_SUBCONTRACTOR_TARGET})\b", re.IGNORECASE)),
    (
        "not accepting c2c-related arrangement",
        re.compile(rf"\bnot\s+accept(?:ing)?\s+{_ANY_TARGET}\b", re.IGNORECASE),
    ),
    (
        "cannot accept c2c-related arrangement",
        re.compile(
            rf"\b(?:cannot|can\s?not|unable\s+to)\s+accept\s+{_ANY_TARGET}\b",
            re.IGNORECASE,
        ),
    ),
    (
        "c2c-related arrangement not permitted",
        re.compile(
            rf"\b{_ANY_TARGET}\s+(?:is\s+|are\s+)?not\s+(?:accepted|allowed|permitted)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "no third-party arrangement permitted",
        re.compile(
            rf"\bno\s+(?:{_THIRD_PARTY_TARGET}|outside|external)\s+"
            rf"(?:{_C2C_TARGET}|{_SUBCONTRACTOR_TARGET}|{_VENDOR_TARGET})\s+"
            rf"(?:is\s+|are\s+)?(?:permitted|allowed|accepted)\b",
            re.IGNORECASE,
        ),
    ),
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
