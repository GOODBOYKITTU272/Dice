"""Phase 4E: maps the existing ApplyWizz candidate-details API response to
DicePilot's normalized CandidateProfile. Data read + normalization only --
no Dice browser logic, no question answering, no application mutation of
any kind.

Source of truth stays the existing ApplyWizz candidate route (per
02_ApplyWizz_DicePilot_TRD.pdf sections 2, 7, 12; 05_...Backend_Schema.pdf
section 14, "Existing ApplyWizz candidate route: GET candidate by
candidate_id -> client + additional_information payload"). This module
does not create a second candidate database -- nothing here is persisted;
every call is a fresh, point-in-time read.

Field mapping follows the TRD's "Candidate Adapter Rules" table (section
7) as closely as the TRD specifies. Two real gaps found during the audit,
neither guessed shut:
  - The TRD's table has no source row for "location" at all -- stays None.
  - "contact_email" is documented only as "Defined product policy... not
    guessed" with no named source field. Mapped from client.email as the
    most consistent read of the client.* identity fields already named
    (client.id, client.full_name, client.visa_type) -- flagged here as an
    inference pending an explicit product decision, not a TRD-confirmed
    field name.
The exact HTTP path (`/candidates/{candidate_id}`) is also not specified
by the TRD/Backend Schema beyond "GET candidate by candidate_id" -- a
reasonable default, not a documented contract; adjust if the real route
differs.

Environment (02_ApplyWizz_DicePilot_TRD.pdf section 12):
  APPLYWIZZ_API_BASE_URL -- existing candidate-details service base URL
  APPLYWIZZ_API_TOKEN    -- service authentication, if required

Never log sensitive candidate payloads wholesale (TRD section 13) --
callers of fetch_candidate() get a CandidateFetchResult with a short,
non-sensitive `error` string, never a raw response body or field values.
"""
from __future__ import annotations

import os

import requests

from dice.models import CandidateFetchResult, CandidateFetchStatus, CandidateProfile

REQUEST_TIMEOUT_SECONDS = 10
_SENSITIVE_FIELDS = {"visa_type", "work_authorized", "requires_sponsorship"}


def _base_url() -> str | None:
    return os.environ.get("APPLYWIZZ_API_BASE_URL")


def _token() -> str | None:
    return os.environ.get("APPLYWIZZ_API_TOKEN")


def _clean_str(value: object) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None  # missing, null, or malformed (non-string) -- never coerced


def _clean_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    return None  # missing, null, or malformed -- never guessed into True/False


def _clean_number(value: object) -> float | int | None:
    if isinstance(value, bool):  # bool is technically an int subclass
        return None
    if isinstance(value, (int, float)):
        return value
    return None  # missing, null, or malformed (e.g. a non-numeric string)


def _resolve_sponsorship(additional: dict, client: dict) -> bool | None:
    primary = _clean_bool(additional.get("require_future_sponsorship"))
    if primary is not None:
        return primary
    return _clean_bool(client.get("sponsorship"))


def normalize_candidate_payload(payload: object) -> CandidateFetchResult:
    """Pure normalization, no HTTP -- independently testable. Maps the
    `{client, additional_information}` response shape to CandidateProfile
    per the TRD's Candidate Adapter Rules table. Unknown/extra fields in
    either source object are ignored, never rejected or copied through."""
    if not isinstance(payload, dict):
        return CandidateFetchResult(CandidateFetchStatus.INVALID_RESPONSE, None, "payload is not an object")

    client = payload.get("client")
    if not isinstance(client, dict):
        return CandidateFetchResult(CandidateFetchStatus.INVALID_RESPONSE, None, "missing or malformed 'client' object")

    additional = payload.get("additional_information")
    if additional is None:
        additional = {}
    if not isinstance(additional, dict):
        return CandidateFetchResult(
            CandidateFetchStatus.INVALID_RESPONSE, None, "'additional_information' is not an object"
        )

    candidate_id = client.get("id")
    if not candidate_id or not isinstance(candidate_id, str):
        return CandidateFetchResult(CandidateFetchStatus.INVALID_RESPONSE, None, "missing or malformed candidate id")

    profile = CandidateProfile(
        candidate_id=candidate_id,
        name=_clean_str(client.get("full_name")),
        email=_clean_str(client.get("email")),
        phone=_clean_str(additional.get("primary_phone")),
        location=None,  # no documented source field -- see module docstring
        visa_type=_clean_str(client.get("visa_type")),
        work_authorized=_clean_bool(additional.get("eligible_to_work_in_us")),
        requires_sponsorship=_resolve_sponsorship(additional, client),
        willing_to_relocate=_clean_bool(additional.get("willing_to_relocate")),
        experience_years=_clean_number(additional.get("experience")),
        desired_start_date=_clean_str(additional.get("desired_start_date")),
        resume_url=_clean_str(additional.get("resume_url")),
        linkedin_url=_clean_str(additional.get("linked_in_url")),
        github_url=_clean_str(additional.get("github_url")),
    )
    return CandidateFetchResult(CandidateFetchStatus.SUCCESS, profile, None)


def fetch_candidate(candidate_id: str) -> CandidateFetchResult:
    """GET candidate by candidate_id from the existing ApplyWizz route,
    normalized to CandidateProfile. Explicit failure states
    (NOT_FOUND/AUTH_ERROR/UPSTREAM_ERROR/INVALID_RESPONSE) instead of a
    generic None -- callers can tell "candidate doesn't exist" apart from
    "couldn't reach the service" apart from "got a response we don't
    understand"."""
    base_url = _base_url()
    if not base_url:
        return CandidateFetchResult(
            CandidateFetchStatus.UPSTREAM_ERROR, None, "APPLYWIZZ_API_BASE_URL is not configured"
        )

    headers = {}
    token = _token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        response = requests.get(
            f"{base_url.rstrip('/')}/candidates/{candidate_id}",
            headers=headers,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.Timeout:
        return CandidateFetchResult(CandidateFetchStatus.UPSTREAM_ERROR, None, "request timed out")
    except requests.RequestException as exc:
        return CandidateFetchResult(CandidateFetchStatus.UPSTREAM_ERROR, None, type(exc).__name__)

    if response.status_code == 404:
        return CandidateFetchResult(CandidateFetchStatus.NOT_FOUND, None, "candidate not found")
    if response.status_code in (401, 403):
        return CandidateFetchResult(CandidateFetchStatus.AUTH_ERROR, None, f"HTTP {response.status_code}")
    if response.status_code != 200:
        return CandidateFetchResult(CandidateFetchStatus.UPSTREAM_ERROR, None, f"HTTP {response.status_code}")

    try:
        payload = response.json()
    except ValueError:
        return CandidateFetchResult(CandidateFetchStatus.INVALID_RESPONSE, None, "response was not valid JSON")

    return normalize_candidate_payload(payload)


def resolve_candidate_field(candidate: CandidateProfile, field_name: str) -> object | None:
    """Minimal read accessor for Phase 4D's future answer-resolution step
    (not wired in yet -- this only defines the contract). Sensitive
    fields are excluded here on purpose: visa_type, work_authorized, and
    requires_sponsorship must keep routing through Phase 4D's existing
    SENSITIVE/NEEDS_INPUT policy, never be auto-resolved from this
    accessor. willing_to_relocate is intentionally NOT a stand-in for an
    "willing to work onsite" question -- those are different concepts and
    Phase 4D's on-site question classifies NEEDS_INPUT regardless of this
    field's value. There is no desired_salary field on CandidateProfile;
    resolving "salary" here would be inventing a field that doesn't
    exist."""
    if field_name in _SENSITIVE_FIELDS:
        return None
    return getattr(candidate, field_name, None)
