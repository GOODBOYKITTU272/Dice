"""Phase 4F: NEEDS_INPUT pause/resume orchestration.

Built entirely on top of the existing Phase 1 schema and repository
(db/application_repository.py; applications, application_events,
interventions) -- no new tables, no new columns, no migration. Every
piece of question-specific metadata this phase needs that has no
dedicated typed column (question_id, field_type, reason, sensitivity)
fits inside the existing interventions.options jsonb column as a
structured wrapper alongside the actual visible choices; answer_source
reuses the existing answered_by column. candidate_id/dice_job_id are
never duplicated onto interventions -- they're already on the parent
applications row via its own columns/FK, so a caller reads them from
there rather than this module storing a second copy.

This module is orchestration/state only: it never touches a live Dice
page, never clicks Next/Review/Submit, and never answers a Dice question
-- resolve_question_intervention() records a human-supplied answer for
later consumption, it does not push anything into Dice.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from db.application_repository import (
    add_event,
    create_intervention,
    get_application,
    resolve_intervention,
)
from db.supabase_client import get_supabase_client

_MISSING_FACT_TYPE = "MISSING_CANDIDATE_FACT"
_UNKNOWN_QUESTION_TYPE = "UNKNOWN_QUESTION"


class InvalidAnswerError(ValueError):
    """Raised when a supplied answer isn't one of the intervention's
    recorded choices (RADIO-shaped interventions only)."""


class AlreadyResolvedError(RuntimeError):
    """Raised when resolve_question_intervention() targets an
    intervention that's already ANSWERED or CANCELLED."""


class InterventionNotFoundError(RuntimeError):
    pass


class ApplicationReadiness(str, Enum):
    READY = "READY"
    RUNNING = "RUNNING"
    NEEDS_INPUT = "NEEDS_INPUT"
    RESUMABLE = "RESUMABLE"
    FAILED = "FAILED"
    SUBMITTED = "SUBMITTED"


# NEEDS_INPUT is handled separately in compute_application_readiness() --
# it's the one status whose readiness depends on intervention state, not
# just the stored applications.status value.
_STATUS_TO_READINESS: dict[str, ApplicationReadiness] = {
    "QUEUED": ApplicationReadiness.READY,
    "PROCESSING": ApplicationReadiness.RUNNING,
    "SUBMITTING": ApplicationReadiness.RUNNING,
    "SUBMITTED": ApplicationReadiness.SUBMITTED,
    "FAILED": ApplicationReadiness.FAILED,
    "FAILED_RETRYABLE": ApplicationReadiness.FAILED,
}


def _question_id_of(intervention_row: dict[str, Any]) -> str | None:
    options = intervention_row.get("options")
    if isinstance(options, dict):
        return options.get("question_id")
    return None


def _find_open_question_intervention(application_id: str, question_id: str) -> dict[str, Any] | None:
    client = get_supabase_client()
    rows = (
        client.table("interventions")
        .select("*")
        .eq("application_id", application_id)
        .eq("status", "OPEN")
        .execute()
        .data
        or []
    )
    for row in rows:
        if _question_id_of(row) == question_id:
            return row
    return None


def create_or_get_question_intervention(
    application_id: str,
    question_id: str,
    question_prompt: str | None,
    field_type: str,
    reason: str,
    choices: list[str] | None = None,
    sensitive: bool = False,
) -> dict[str, Any]:
    """Record that a Dice question blocks this application, or return the
    already-open intervention for that exact question unchanged.

    Idempotent by design: a worker restart re-encountering the same
    blocking question (application_id, question_id) must never create a
    second OPEN row -- this is the phase's required dedupe behavior,
    implemented as a check-before-insert rather than a DB constraint
    (matches this project's existing single-worker-V1 tradeoff, same as
    application_repository.create_intervention()'s own two-sequential-
    calls note).

    Always APPLICATION_LEVEL: an unanswerable question blocks only this
    one application, never the whole candidate/browser session (that
    remains AUTHENTICATION/SECURITY_ACTION's job, unchanged from Phase 1).
    """
    existing = _find_open_question_intervention(application_id, question_id)
    if existing is not None:
        return existing

    intervention_type = _UNKNOWN_QUESTION_TYPE if field_type == "UNSUPPORTED" else _MISSING_FACT_TYPE
    options_payload = {
        "question_id": question_id,
        "field_type": field_type,
        "reason": reason,
        "sensitivity": sensitive,
        "choices": choices,
    }

    # application_repository.create_intervention() always tries to
    # transition the application to NEEDS_INPUT, but NEEDS_INPUT ->
    # NEEDS_INPUT isn't a modeled transition (STATUS_TRANSITIONS has no
    # self-loop). A second, different blocking question on an
    # application that's already NEEDS_INPUT must still get its own
    # intervention row -- insert directly and skip the redundant
    # transition rather than calling create_intervention().
    if get_application(application_id)["status"] == "NEEDS_INPUT":
        client = get_supabase_client()
        result = (
            client.table("interventions")
            .insert(
                {
                    "application_id": application_id,
                    "type": intervention_type,
                    "intervention_scope": "APPLICATION_LEVEL",
                    "question_text": question_prompt,
                    "options": options_payload,
                }
            )
            .execute()
        )
        row = result.data[0]
    else:
        row = create_intervention(
            application_id=application_id,
            intervention_type=intervention_type,
            intervention_scope="APPLICATION_LEVEL",
            question_text=question_prompt,
            options=options_payload,
        )
    add_event(
        application_id,
        event_type="needs_input",
        step="ANSWER_QUESTIONS",
        message=f"blocked on question {question_id!r}: {reason}",
        metadata={"question_id": question_id, "field_type": field_type, "sensitivity": sensitive},
    )
    return row


def resolve_question_intervention(intervention_id: str, answer: Any, source: str) -> dict[str, Any]:
    """OPEN -> ANSWERED exactly once. The answer is stored exactly as
    supplied -- no LLM rewriting, no coercion, no stripping. A RADIO-
    shaped intervention rejects any answer not present in its recorded
    choices rather than accepting and silently normalizing it. This does
    not push the answer into Dice or resume the application; that stays
    a later phase's job."""
    client = get_supabase_client()
    rows = client.table("interventions").select("*").eq("id", intervention_id).execute().data
    if not rows:
        raise InterventionNotFoundError(intervention_id)
    row = rows[0]

    if row["status"] != "OPEN":
        raise AlreadyResolvedError(f"intervention {intervention_id} is already {row['status']}")

    options = row.get("options") or {}
    if options.get("field_type") == "RADIO":
        choices = options.get("choices")
        if choices and answer not in choices:
            raise InvalidAnswerError(f"{answer!r} is not one of the recorded options {choices!r}")

    resolved = resolve_intervention(intervention_id, answer, source)
    add_event(
        row["application_id"],
        event_type="intervention_resolved",
        step="ANSWER_QUESTIONS",
        message=f"resolved question {options.get('question_id')!r}",
        metadata={"question_id": options.get("question_id"), "source": source},
    )
    return resolved


def compute_application_readiness(application_id: str) -> ApplicationReadiness:
    """Read-only, derived state -- RESUMABLE is never written to
    applications.status (the existing schema's CHECK constraint has no
    such value, and this phase does not migrate the schema). An
    application in NEEDS_INPUT is reported RESUMABLE here only once every
    one of its OPEN interventions has been resolved; the stored
    applications.status row stays NEEDS_INPUT until a worker (not built
    in this phase) explicitly transitions it to PROCESSING to actually
    resume execution."""
    application = get_application(application_id)
    status = application["status"]

    if status == "NEEDS_INPUT":
        client = get_supabase_client()
        open_rows = (
            client.table("interventions")
            .select("id")
            .eq("application_id", application_id)
            .eq("status", "OPEN")
            .execute()
            .data
        )
        return ApplicationReadiness.NEEDS_INPUT if open_rows else ApplicationReadiness.RESUMABLE

    return _STATUS_TO_READINESS[status]
