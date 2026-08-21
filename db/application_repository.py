"""Minimum Supabase repository operations needed for DicePilot Phase 1.

No browser/Dice.com logic here — this module only talks to Postgres via
the Supabase client. The atomic queue claim is a single SQL statement
(see supabase/migrations/20260820175616_dicepilot_foundation.sql,
claim_next_queued_application) rather than an in-memory Python lock, so it
stays correct even with multiple worker processes later.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from db.supabase_client import get_supabase_client

UNIQUE_VIOLATION = "23505"

# From Backend Schema doc §12. Enforced here so an invalid transition never
# reaches the database silently disguised as a normal update.
STATUS_TRANSITIONS: dict[str, set[str]] = {
    "QUEUED": {"PROCESSING"},
    "PROCESSING": {"NEEDS_INPUT", "SUBMITTING", "FAILED_RETRYABLE", "FAILED"},
    "NEEDS_INPUT": {"PROCESSING", "FAILED"},
    "SUBMITTING": {"SUBMITTED", "FAILED_RETRYABLE", "FAILED"},
    "FAILED_RETRYABLE": {"QUEUED", "PROCESSING"},
    "SUBMITTED": set(),
    "FAILED": set(),
}

# From V1 Decision 2. APPLICATION_LEVEL blocks only its own application;
# SESSION_LEVEL blocks the whole candidate/browser worker until resolved.
# See claim_next_queued_application() in the migration for enforcement.
INTERVENTION_SCOPES = {"APPLICATION_LEVEL", "SESSION_LEVEL"}


class DuplicateApplicationError(RuntimeError):
    """Raised when (candidate_id, dice_job_id) already has an application row."""


class InvalidInterventionScopeError(ValueError):
    """Raised when intervention_scope isn't one of INTERVENTION_SCOPES."""


class InvalidStatusTransitionError(RuntimeError):
    """Raised when an update_application_status call violates the status model."""

    def __init__(self, current: str, requested: str):
        super().__init__(f"cannot transition application from {current!r} to {requested!r}")
        self.current = current
        self.requested = requested


class ApplicationNotFoundError(RuntimeError):
    pass


class DiceJobNotFoundError(RuntimeError):
    pass


def _is_unique_violation(exc: Exception) -> bool:
    return getattr(exc, "code", None) == UNIQUE_VIOLATION


def upsert_dice_job(job: dict[str, Any]) -> dict[str, Any]:
    """Create or refresh a canonical dice_jobs row, keyed on dice_job_id."""
    client = get_supabase_client()
    result = (
        client.table("dice_jobs")
        .upsert(job, on_conflict="dice_job_id")
        .execute()
    )
    return result.data[0]


def get_dice_job(dice_job_id: str) -> dict[str, Any]:
    """dice_job_id here is dice_jobs.id (the FK applications.dice_job_id
    points at), not the raw Dice UUID text column."""
    client = get_supabase_client()
    result = client.table("dice_jobs").select("*").eq("id", dice_job_id).execute()
    if not result.data:
        raise DiceJobNotFoundError(dice_job_id)
    return result.data[0]


def enqueue_application(candidate_id: str, dice_job_id: str) -> dict[str, Any]:
    """Create a QUEUED application row for (candidate_id, dice_job_id).

    Raises DuplicateApplicationError instead of silently upserting, so a
    caller can never accidentally re-queue a job the candidate already has.
    """
    client = get_supabase_client()
    try:
        result = (
            client.table("applications")
            .insert({"candidate_id": candidate_id, "dice_job_id": dice_job_id})
            .execute()
        )
    except Exception as exc:
        if _is_unique_violation(exc):
            raise DuplicateApplicationError(
                f"application already exists for candidate={candidate_id} job={dice_job_id}"
            ) from exc
        raise
    return result.data[0]


def claim_next_queued_application(candidate_id: str, worker_id: str) -> dict[str, Any] | None:
    """Atomically claim the next QUEUED application for one candidate.

    Backed by the claim_next_queued_application() Postgres function so the
    claim is a single atomic statement, safe under concurrent callers.
    Returns None if there's nothing eligible to claim right now (including
    when the candidate already has a PROCESSING/SUBMITTING application).
    """
    client = get_supabase_client()
    result = client.rpc(
        "claim_next_queued_application",
        {"p_candidate_id": candidate_id, "p_worker_id": worker_id},
    ).execute()
    rows = result.data or []
    return rows[0] if rows else None


def claim_next_queued_application_for_run(run_id: str, worker_id: str) -> dict[str, Any] | None:
    """Same atomic-claim guarantee as claim_next_queued_application(), but
    scoped to one bounded run_id (migration 20260822010000) instead of a
    whole candidate's queue -- this is what lets the worker process
    exactly a Jobs-selection batch and structurally never drift into an
    unrelated QUEUED application, no matter what else exists for the same
    candidate."""
    client = get_supabase_client()
    result = client.rpc(
        "claim_next_queued_application_for_run",
        {"p_run_id": run_id, "p_worker_id": worker_id},
    ).execute()
    rows = result.data or []
    return rows[0] if rows else None


def get_application(application_id: str) -> dict[str, Any]:
    client = get_supabase_client()
    result = (
        client.table("applications").select("*").eq("id", application_id).execute()
    )
    if not result.data:
        raise ApplicationNotFoundError(application_id)
    return result.data[0]


def update_application_status(
    application_id: str, new_status: str, **fields: Any
) -> dict[str, Any]:
    """Update an application's status, enforcing the V1 status transition map.

    NEEDS_INPUT is a normal stop, never silently rewritten to FAILED — the
    caller must explicitly request FAILED if that's the intended outcome.
    """
    current = get_application(application_id)["status"]
    allowed = STATUS_TRANSITIONS.get(current, set())
    if new_status not in allowed:
        raise InvalidStatusTransitionError(current, new_status)

    client = get_supabase_client()
    payload = {"status": new_status, **fields}
    result = (
        client.table("applications")
        .update(payload)
        .eq("id", application_id)
        .execute()
    )
    return result.data[0]


def add_event(
    application_id: str,
    event_type: str,
    step: str | None = None,
    message: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    client = get_supabase_client()
    result = (
        client.table("application_events")
        .insert(
            {
                "application_id": application_id,
                "event_type": event_type,
                "step": step,
                "message": message,
                "metadata": metadata,
            }
        )
        .execute()
    )
    return result.data[0]


def create_intervention(
    application_id: str,
    intervention_type: str,
    intervention_scope: str,
    question_text: str | None = None,
    options: list[Any] | None = None,
) -> dict[str, Any]:
    """Record an intervention and move the application to NEEDS_INPUT.

    intervention_scope is required, not defaulted — APPLICATION_LEVEL vs
    SESSION_LEVEL changes queue-claim behavior (see the migration's
    claim_next_queued_application()), so callers must decide explicitly.

    Both scopes move *this* application to NEEDS_INPUT — V1's status model
    has no separate "session blocked" application status. What differs is
    whether claim_next_queued_application() lets the worker move on to a
    different QUEUED application while this one waits.

    These are two sequential calls, not one transaction — acceptable for
    Phase 1 since only one worker exists; revisit if that stops being true.
    """
    if intervention_scope not in INTERVENTION_SCOPES:
        raise InvalidInterventionScopeError(intervention_scope)

    client = get_supabase_client()
    result = (
        client.table("interventions")
        .insert(
            {
                "application_id": application_id,
                "type": intervention_type,
                "intervention_scope": intervention_scope,
                "question_text": question_text,
                "options": options,
            }
        )
        .execute()
    )
    update_application_status(application_id, "NEEDS_INPUT")
    return result.data[0]


def requeue_failed_application(application_id: str) -> dict[str, Any]:
    """Manual, explicit FAILED_RETRYABLE -> QUEUED requeue.

    V1 has no automatic retry anywhere — this is the only path back to
    QUEUED from a failure, and it only runs when a caller (operator action,
    future retry endpoint) deliberately invokes it. Bumps attempt_count and
    clears worker ownership so the next claim starts clean.
    """
    current = get_application(application_id)
    return update_application_status(
        application_id,
        "QUEUED",
        worker_id=None,
        lock_acquired_at=None,
        attempt_count=current["attempt_count"] + 1,
        queued_at=datetime.now(timezone.utc).isoformat(),
    )


def resolve_intervention(
    intervention_id: str, answer: Any, answered_by: str
) -> dict[str, Any]:
    """Mark an intervention ANSWERED. Does not itself resume the application —
    that decision belongs to worker code, which doesn't exist yet in Phase 1.
    """
    client = get_supabase_client()
    result = (
        client.table("interventions")
        .update(
            {
                "status": "ANSWERED",
                "answer": answer,
                "answered_by": answered_by,
                "resolved_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        .eq("id", intervention_id)
        .execute()
    )
    return result.data[0]
