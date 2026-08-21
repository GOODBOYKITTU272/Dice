"""Phase 5: the DB-side half of submission verification, kept
deliberately separate from dice_browser/submission.py's browser-side
classification (Part 5's "keep database state transition separate and
explicit"). applications.status is written to SUBMITTED only when the
browser layer already returned VERIFIED_SUBMITTED -- this module never
re-derives or second-guesses that classification, it only records it.

Built on the existing Phase 1 repository/schema (applications,
application_events) -- no migration, no new table.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from db.application_repository import add_event, update_application_status
from dice_browser.models import SubmissionResult, SubmissionStatus


def record_submission_result(application_id: str, result: SubmissionResult) -> dict[str, Any] | None:
    """Always writes an application_event capturing the classified
    result (status, reason, and the same bounded, non-sensitive evidence
    dict submission.py produced -- never a raw page dump). Only
    transitions applications.status -> SUBMITTED when
    result.status == VERIFIED_SUBMITTED; every other outcome
    (NOT_SUBMITTED, VERIFICATION_UNCERTAIN, AUTH_REQUIRED, NEEDS_INPUT,
    SECURITY_CHALLENGE, SUBMIT_FAILED) leaves applications.status
    untouched and returns None -- there is no automatic retry or
    downgrade path here."""
    add_event(
        application_id,
        event_type="submission_result",
        step="VERIFY_SUCCESS",
        message=result.reason,
        metadata={"status": result.status, "evidence": result.evidence},
    )

    if result.status != SubmissionStatus.VERIFIED_SUBMITTED:
        return None

    return update_application_status(
        application_id,
        "SUBMITTED",
        submitted_at=datetime.now(timezone.utc).isoformat(),
        verification_evidence=result.evidence,
    )
