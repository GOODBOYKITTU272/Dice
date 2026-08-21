"""Normalized data structures for Dice discovery (Phase 2).

DiceJobRecord maps 1:1 onto the existing dice_jobs table columns from
Phase 1 (supabase/migrations/20260820175616_dicepilot_foundation.sql) — no
schema change was needed for Phase 2. c2c_reason doubles as the "C2C
evidence" field the local UI shows; easy_apply_evidence (jsonb) holds where
the Easy Apply signal came from.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class RawSearchResult:
    """One job card as scraped from a Dice search results page."""

    dice_job_id: str
    title: str
    company_name: str | None
    location: str | None
    canonical_url: str
    employment_type_text: str  # raw badge text, e.g. "Contract, Third Party"
    easy_apply_badge_present: bool


@dataclass
class JobDetail:
    """Parsed job detail. title/description/employment_type/company_name/
    date_posted/canonical_url come from our own JSON-LD parse or upstream's
    __NEXT_DATA__ fallback (dice/job_parser.py decides which). salary_text
    and experience_text are Phase 3A additions, upstream-derived, optional.
    """

    title: str
    description_html: str
    description_text: str
    employment_type: str | None  # schema.org value, e.g. "CONTRACTOR"
    company_name: str | None
    date_posted: str | None
    canonical_url: str
    salary_text: str | None = None
    experience_text: str | None = None


@dataclass
class C2CResult:
    status: str  # CONFIRMED | LIKELY | NOT_C2C | UNKNOWN
    reason: str
    evidence: list[str] = field(default_factory=list)


@dataclass
class EasyApplyResult:
    is_easy_apply: bool
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class DiceJobRecord:
    """The row shape written to dice_jobs via upsert_dice_job()."""

    dice_job_id: str
    canonical_url: str
    title: str
    company_name: str | None
    location: str | None
    employment_type: str | None
    is_third_party: bool
    description: str
    c2c_status: str
    c2c_reason: str
    is_easy_apply: bool
    easy_apply_evidence: dict[str, Any]
    discovered_at: str
    raw_metadata: dict[str, Any] | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "dice_job_id": self.dice_job_id,
            "canonical_url": self.canonical_url,
            "title": self.title,
            "company_name": self.company_name,
            "location": self.location,
            "employment_type": self.employment_type,
            "is_third_party": self.is_third_party,
            "description": self.description,
            "c2c_status": self.c2c_status,
            "c2c_reason": self.c2c_reason,
            "is_easy_apply": self.is_easy_apply,
            "easy_apply_evidence": self.easy_apply_evidence,
            "discovered_at": self.discovered_at,
            "raw_metadata": self.raw_metadata,
        }


class CandidateFetchStatus:
    """String constants, not an Enum -- matches this file's existing
    plain-string-status convention (C2CResult.status, EasyApplyResult
    etc.)."""

    SUCCESS = "SUCCESS"
    NOT_FOUND = "NOT_FOUND"
    AUTH_ERROR = "AUTH_ERROR"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    INVALID_RESPONSE = "INVALID_RESPONSE"


@dataclass
class CandidateProfile:
    """Normalized candidate facts for DicePilot, mapped from the existing
    ApplyWizz candidate-details API response (02_ApplyWizz_DicePilot_TRD.pdf
    section 7, "Candidate Adapter Rules"). This is not a second candidate
    database -- candidate_id is the only durable reference; every other
    field is a point-in-time read, re-fetched per use, never persisted as
    DicePilot's own source of truth.

    Every Optional field is None when the source payload doesn't have a
    usable value -- missing, null, or malformed input is never defaulted,
    guessed, or coerced into False/0/"" by dice/candidate_adapter.py."""

    candidate_id: str
    name: str | None
    email: str | None
    phone: str | None
    location: str | None
    visa_type: str | None
    work_authorized: bool | None
    requires_sponsorship: bool | None
    willing_to_relocate: bool | None
    experience_years: float | int | None
    desired_start_date: str | None
    resume_url: str | None
    linkedin_url: str | None
    github_url: str | None


@dataclass
class CandidateFetchResult:
    """Result of dice.candidate_adapter.fetch_candidate(). profile is
    only set when status == SUCCESS. error is a short, non-sensitive
    diagnostic string -- never a raw response body or candidate payload."""

    status: str  # one of CandidateFetchStatus's constants
    profile: CandidateProfile | None
    error: str | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
