"""Phase 6: standalone sequential Dice self-apply worker.

Glue only -- every real capability already exists and is reused
unmodified: discovery (Phase 2), candidate fetch (dice.candidate_adapter,
Phase 4E), question extraction/classification (dice_browser.questions,
Phase 4D), pause/resume (db.intervention_repository, Phase 4F), and
submission verification (dice_browser.submission, Phase 5). This module
owns only the sequencing, plus the two narrow new actions Phase 6 itself
needed: dice_browser.wizard_navigation (filling an already-resolved
answer, clicking Next) and dice.answer_resolution (a currently-empty,
narrow safe-auto-answer map).

MUST be run as a standalone process (`python -m dice_browser.worker`),
never invoked as an automated action inside an AI coding session in this
project -- every real Dice mutation attempted that way this session
(Submit, selecting a radio answer) was blocked by the environment's own
permission classifier, a deliberate guardrail that was never worked
around. A human runs this script themselves, in their own terminal, and
watches it.

Default submission policy is REQUIRE_CONFIRMATION: reaching Review never
by itself triggers Submit. AUTHORIZED_AUTONOMOUS is architected (the code
path exists and is unit-tested) but is not enabled or exercised live as
of Phase 6's creation -- turning it on is a deliberate, separate decision
for later, not a default.

Known gap, not hidden: this module does not reclaim a stale PROCESSING
lock left behind by a crashed prior worker run. The only unstick path
today is the existing, manual db.application_repository.requeue_failed_application()
for FAILED_RETRYABLE rows -- a genuinely stuck PROCESSING row has no
automated recovery yet. Idempotency here means: re-running the worker
never claims a second application concurrently (the existing atomic
Postgres claim already guarantees that), never creates a duplicate
intervention for the same question (Phase 4F's own idempotent
check-before-insert), and never re-submits or double-writes SUBMITTED
(Phase 5's own guard) -- not that a crashed run's in-flight application
self-heals.
"""
from __future__ import annotations

import argparse
import os
import sys
import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

from playwright.sync_api import Page

import run_registry
from db.application_repository import (
    add_event,
    claim_next_queued_application,
    claim_next_queued_application_for_run,
    get_application,
    get_dice_job,
    update_application_status,
)
from db.intervention_repository import (
    ApplicationReadiness,
    compute_application_readiness,
    create_or_get_question_intervention,
    find_reusable_answer,
    get_resolved_answers,
)
from db.submission_repository import record_submission_result
from dice.answer_resolution import resolve_safe_answer
from dice.candidate_adapter import fetch_candidate
from dice.models import CandidateFetchStatus, CandidateProfile
from dice_browser.easy_apply import open_easy_apply
from dice_browser.models import FieldType, QuestionExtractionStatus, QuestionStatus, SubmissionResult, SubmissionStatus
from dice_browser.navigator import open_job
from dice_browser.questions import extract_questions, is_review_screen
from dice_browser.resume import detect_existing_resume, upload_resume
from dice_browser.submission import SubmitPreconditions, submit_application
from dice_browser.wizard_navigation import AnswerFillFailedError, UnsupportedFieldTypeError, click_next, fill_answer


class SubmissionPolicy(str, Enum):
    REQUIRE_CONFIRMATION = "REQUIRE_CONFIRMATION"
    AUTHORIZED_AUTONOMOUS = "AUTHORIZED_AUTONOMOUS"


class StopReason(str, Enum):
    NOTHING_QUEUED = "NOTHING_QUEUED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    SECURITY_CHALLENGE = "SECURITY_CHALLENGE"
    ALREADY_APPLIED = "ALREADY_APPLIED"
    STALE_OR_INELIGIBLE = "STALE_OR_INELIGIBLE"
    EASY_APPLY_OPEN_FAILED = "EASY_APPLY_OPEN_FAILED"
    RESUME_UPLOAD_FAILED = "RESUME_UPLOAD_FAILED"
    NAVIGATION_FAILED = "NAVIGATION_FAILED"
    NEEDS_INPUT = "NEEDS_INPUT"
    NOT_YET_RESUMABLE = "NOT_YET_RESUMABLE"
    AWAITING_SUBMIT_CONFIRMATION = "AWAITING_SUBMIT_CONFIRMATION"
    VERIFICATION_UNCERTAIN = "VERIFICATION_UNCERTAIN"
    SUBMIT_FAILED = "SUBMIT_FAILED"
    VERIFIED_SUBMITTED = "VERIFIED_SUBMITTED"
    PROOF_STOP_EASY_APPLY_OPENED = "PROOF_STOP_EASY_APPLY_OPENED"


# Stops that mean something is wrong at the session/worker level, not
# specific to one job -- these count toward the circuit breaker.
_SESSION_LEVEL_STOPS = {StopReason.AUTH_REQUIRED, StopReason.SECURITY_CHALLENGE}


@dataclass
class ApplicationRunResult:
    application_id: str | None
    dice_job_id: str | None
    stop_reason: StopReason
    detail: str


@dataclass
class WorkerRunSummary:
    processed: list[ApplicationRunResult] = field(default_factory=list)
    halted: bool = False
    halt_reason: str | None = None


def _fail(application_id: str, status: str, error_code: str, error_message: str) -> None:
    update_application_status(application_id, status, error_code=error_code, error_message=error_message)


_PROOF_STOP_ENV_VAR = "DICEPILOT_PROOF_STOP_AFTER_EASY_APPLY_OPEN"


def _proof_stop_after_easy_apply_open() -> bool:
    """Env-gated, default-off. For live-proving the Telegram->Browserless
    bridge reaches the real Easy Apply wizard without ever risking a real
    resume upload, question fill, or Submit. Deliberately never committed
    enabled -- read fresh from the environment each call, no code-level
    default other than False."""
    return os.environ.get(_PROOF_STOP_ENV_VAR, "").strip().lower() in ("1", "true", "yes")


def _load_candidate(candidate_id: str) -> CandidateProfile | None:
    result = fetch_candidate(candidate_id)
    return result.profile if result.status == CandidateFetchStatus.SUCCESS else None


def _walk_questions_to_review(
    page: Page,
    application_id: str,
    candidate_id: str,
    candidate: CandidateProfile | None,
    resolved_overrides: dict[str, Any],
) -> ApplicationRunResult | None:
    """Advances through question step(s) until Review is reached or a
    blocker stops progress. Returns None on success (Review reached);
    an ApplicationRunResult if something stopped it. `resolved_overrides`
    are question_id -> answer pairs already resolved via a Supabase
    intervention (the resume path) -- these are filled directly, never
    re-asked. Failing that, find_reusable_answer() (2026-08-24) checks
    whether this SAME candidate already answered this exact standardized
    Dice question_id on a different application -- see its own docstring
    for why this can never accidentally reuse a job-specific question."""
    while True:
        if is_review_screen(page):
            return None

        extraction = extract_questions(page)

        if extraction.status == QuestionExtractionStatus.NO_QUESTIONS_PRESENT:
            if not click_next(page):
                return ApplicationRunResult(application_id, None, StopReason.NAVIGATION_FAILED, "no further step and no Review reached")
            continue

        if extraction.status == QuestionExtractionStatus.UNKNOWN_SCREEN:
            _fail(application_id, "FAILED_RETRYABLE", "UNKNOWN_SCREEN", "wizard screen not recognized")
            return ApplicationRunResult(application_id, None, StopReason.NAVIGATION_FAILED, "unrecognized wizard screen")

        blocked = False
        for question in extraction.questions:
            if question.status == QuestionStatus.ALREADY_ANSWERED:
                continue

            override = resolved_overrides.get(question.question_id)
            if override is not None:
                try:
                    fill_answer(page, question, override)
                except (AnswerFillFailedError, UnsupportedFieldTypeError) as exc:
                    _fail(application_id, "FAILED_RETRYABLE", "ANSWER_FILL_FAILED", str(exc))
                    return ApplicationRunResult(application_id, None, StopReason.NAVIGATION_FAILED, str(exc))
                add_event(
                    application_id,
                    event_type="answer_filled_from_resolved_intervention",
                    step="ANSWER_QUESTIONS",
                    message=f"filled resolved answer for {question.question_id}",
                    metadata={"question_id": question.question_id},
                )
                continue

            safe_answer = None
            if candidate is not None and question.status == QuestionStatus.NEEDS_INPUT:
                safe_answer = resolve_safe_answer(question, candidate)

            if safe_answer is not None:
                try:
                    fill_answer(page, question, safe_answer)
                except (AnswerFillFailedError, UnsupportedFieldTypeError) as exc:
                    _fail(application_id, "FAILED_RETRYABLE", "ANSWER_FILL_FAILED", str(exc))
                    return ApplicationRunResult(application_id, None, StopReason.NAVIGATION_FAILED, str(exc))
                add_event(
                    application_id,
                    event_type="answer_auto_filled",
                    step="ANSWER_QUESTIONS",
                    message=f"auto-filled {question.question_id} from a trusted candidate field",
                    metadata={"question_id": question.question_id},
                )
                continue

            reused_answer = None
            if question.status == QuestionStatus.NEEDS_INPUT:
                reused_answer = find_reusable_answer(candidate_id, question.question_id)

            if reused_answer is not None:
                try:
                    fill_answer(page, question, reused_answer)
                except (AnswerFillFailedError, UnsupportedFieldTypeError) as exc:
                    _fail(application_id, "FAILED_RETRYABLE", "ANSWER_FILL_FAILED", str(exc))
                    return ApplicationRunResult(application_id, None, StopReason.NAVIGATION_FAILED, str(exc))
                add_event(
                    application_id,
                    event_type="answer_reused_from_prior_application",
                    step="ANSWER_QUESTIONS",
                    message=f"reused a prior answer this candidate already gave for {question.question_id}",
                    metadata={"question_id": question.question_id},
                )
                continue

            reason = (
                "no trusted candidate mapping"
                if question.status == QuestionStatus.NEEDS_INPUT
                else f"unsupported control type ({question.field_type})"
            )
            create_or_get_question_intervention(
                application_id=application_id,
                question_id=question.question_id,
                question_prompt=question.prompt,
                field_type=question.field_type,
                reason=reason,
                choices=list(question.options) if question.options else None,
                sensitive=False,
            )
            blocked = True

        if blocked:
            return ApplicationRunResult(application_id, None, StopReason.NEEDS_INPUT, "one or more questions need human input")

        if not click_next(page):
            return ApplicationRunResult(application_id, None, StopReason.NAVIGATION_FAILED, "no further step and no Review reached")


def _gate_and_maybe_submit(
    page: Page,
    application_id: str,
    dice_job_id: str,
    canonical_url: str,
    submission_policy: SubmissionPolicy,
) -> ApplicationRunResult:
    if not is_review_screen(page):
        _fail(application_id, "FAILED_RETRYABLE", "REVIEW_NOT_REACHED", "wizard did not reach a recognized Review screen")
        return ApplicationRunResult(application_id, dice_job_id, StopReason.NAVIGATION_FAILED, "did not reach Review")

    if submission_policy == SubmissionPolicy.REQUIRE_CONFIRMATION:
        add_event(
            application_id,
            event_type="awaiting_submit_confirmation",
            step="NEXT_OR_REVIEW",
            message="Review reached; awaiting explicit human confirmation before Submit",
        )
        # Real bug found via live-Supabase bounded-run testing (2026-08-22):
        # leaving this application at PROCESSING forever meant
        # claim_next_queued_application()'s own "no other PROCESSING/
        # SUBMITTING application for this candidate" gate would then
        # permanently block every later claim for that candidate -- a
        # multi-job run under REQUIRE_CONFIRMATION could only ever process
        # its first job. NEEDS_INPUT is the existing, already-modeled
        # "doesn't block sibling claims" status (see this project's own
        # claim RPC comment: "does NOT block on APPLICATION_LEVEL
        # NEEDS_INPUT") -- reused here rather than adding a new status
        # value and migration. No interventions row is created; a
        # NEEDS_INPUT application with zero open interventions is exactly
        # what "awaiting a human's go/no-go on Submit" looks like.
        update_application_status(application_id, "NEEDS_INPUT")
        return ApplicationRunResult(application_id, dice_job_id, StopReason.AWAITING_SUBMIT_CONFIRMATION, "awaiting human confirmation")

    return _submit_with_verification(page, application_id, dice_job_id, canonical_url)


def _resolve_uncertain_via_already_applied(page: Page, canonical_url: str, result: SubmissionResult) -> SubmissionResult:
    """Live-found 2026-08-24: submission.py's text-match confirmation
    check can miss a genuinely successful submit (Dice's confirmation
    banner not yet rendered within the poll window), leaving a real
    application stuck as VERIFICATION_UNCERTAIN with no automatic path
    to SUBMITTED -- previously required a human to manually re-check the
    live job page. Dice's own already_applied signal (the same
    read-only, no-click check navigator.open_job already does before
    ever starting a wizard) is authoritative and cannot false-positive
    here: this job was confirmed NOT already_applied earlier in this
    same run, before Easy Apply was opened, so True now can only be a
    result of the submit attempt just made. Anything other than a clean
    True (False, None, or the re-check itself erroring) leaves `result`
    untouched -- genuine ambiguity still falls through to
    FAILED_RETRYABLE for human review, never guessed."""
    try:
        recheck = open_job(page, canonical_url)
    except Exception:  # noqa: BLE001 - best-effort fallback; never let it crash a real submit result
        return result

    if recheck.already_applied is not True:
        return result

    return replace(
        result,
        status=SubmissionStatus.VERIFIED_SUBMITTED,
        reason="confirmation text was not detected, but a live re-check confirmed Dice now reports this job as already applied",
        evidence={
            **result.evidence,
            "fallback_check": "already_applied_recheck",
            "original_reason": result.reason,
        },
    )


def _submit_with_verification(page: Page, application_id: str, dice_job_id: str, canonical_url: str) -> ApplicationRunResult:
    """AUTHORIZED_AUTONOMOUS path. Never retries: whatever
    submit_application() classifies is recorded once and this function
    returns.

    Regression, live-found 2026-08-22 (first genuine live exercise of
    this path): submit_application's expected_job_url_fragment must be
    the job id fragment that actually appears in the live wizard URL
    (.../job-applications/{fragment}/wizard) -- passing canonical_url
    (the job-DETAIL page, .../job-detail/{fragment}) whole made its own
    pre-submit URL gate fail every time, since "job-detail" !=
    "job-applications" even though both contain the same fragment.
    canonical_url's own last path segment (validate_canonical_url
    guarantees the /job-detail/{fragment} shape) is that fragment."""
    update_application_status(application_id, "SUBMITTING")
    preconditions = SubmitPreconditions(authenticated=True, no_unresolved_interventions=True, already_verified_submitted=False)
    job_url_fragment = canonical_url.rstrip("/").rsplit("/", 1)[-1]
    result = submit_application(page, job_url_fragment, application_id, dice_job_id, preconditions)
    if result.status == SubmissionStatus.VERIFICATION_UNCERTAIN:
        result = _resolve_uncertain_via_already_applied(page, canonical_url, result)
    record_submission_result(application_id, result)

    # Regression, live-found 2026-08-22: record_submission_result() only
    # ever writes applications.status on VERIFIED_SUBMITTED (by design --
    # see its own docstring). Every other SubmissionStatus left the
    # application stuck at SUBMITTING forever, with no path back to
    # QUEUED (SUBMITTING isn't even a valid source state for QUEUED in
    # STATUS_TRANSITIONS) -- a real, previously-latent gap, only ever
    # exercised by this phase's first genuine live Submit attempt.
    # SUBMIT_FAILED is Dice explicitly rejecting the submission -> FAILED,
    # no automatic retry. Everything else (VERIFICATION_UNCERTAIN,
    # AUTH_REQUIRED, SECURITY_CHALLENGE, NEEDS_INPUT, NOT_SUBMITTED --
    # live re-checks inside submit_application can still surface any of
    # these even though this function hardcodes its own preconditions
    # true) is FAILED_RETRYABLE, so a human can requeue_failed_application()
    # once the underlying condition (auth, challenge, etc.) has cleared.
    if result.status == SubmissionStatus.SUBMIT_FAILED:
        _fail(application_id, "FAILED", result.status.value, result.reason)
    elif result.status != SubmissionStatus.VERIFIED_SUBMITTED:
        _fail(application_id, "FAILED_RETRYABLE", result.status.value, result.reason)

    if result.status == SubmissionStatus.VERIFIED_SUBMITTED:
        return ApplicationRunResult(application_id, dice_job_id, StopReason.VERIFIED_SUBMITTED, result.reason)
    if result.status == SubmissionStatus.SUBMIT_FAILED:
        return ApplicationRunResult(application_id, dice_job_id, StopReason.SUBMIT_FAILED, result.reason)
    return ApplicationRunResult(application_id, dice_job_id, StopReason.VERIFICATION_UNCERTAIN, result.reason)


def process_one_application(
    page: Page,
    candidate_id: str,
    worker_id: str,
    submission_policy: SubmissionPolicy = SubmissionPolicy.REQUIRE_CONFIRMATION,
    resume_path=None,
    claim_fn=None,
) -> ApplicationRunResult:
    """Claims and fully processes exactly one QUEUED application --
    never claims a second one. run_worker() calls this in a loop.

    claim_fn, when given, replaces the default "claim the oldest QUEUED
    application for this candidate" behavior -- run_worker_for_run() uses
    it to claim one specific, pre-selected application_id instead of
    querying the DB pool. Every gate below this point (live requalify,
    Easy Apply, resume, questions, Review, submit) is unchanged and
    shared by both paths."""
    application = claim_fn() if claim_fn is not None else claim_next_queued_application(candidate_id, worker_id)
    if application is None:
        return ApplicationRunResult(None, None, StopReason.NOTHING_QUEUED, "no QUEUED application available")

    application_id = application["id"]
    dice_job_id = application["dice_job_id"]
    add_event(application_id, event_type="worker_claimed", step="CLAIM", message=worker_id)

    try:
        dice_job = get_dice_job(dice_job_id)
    except Exception as exc:
        _fail(application_id, "FAILED", "DICE_JOB_NOT_FOUND", str(exc))
        return ApplicationRunResult(application_id, dice_job_id, StopReason.NAVIGATION_FAILED, str(exc))

    canonical_url = dice_job["canonical_url"]

    # Re-check live eligibility -- never trust stale discovery data (the
    # TalentFish finding from Phase 5's audit: stored qualification can
    # go stale between discovery time and application time).
    nav_result = open_job(page, canonical_url)
    if not nav_result.authenticated:
        _fail(application_id, "FAILED_RETRYABLE", "AUTH_REQUIRED", "not authenticated on live re-check")
        return ApplicationRunResult(application_id, dice_job_id, StopReason.AUTH_REQUIRED, "not authenticated")
    if nav_result.challenge_type is not None:
        _fail(application_id, "FAILED_RETRYABLE", "SECURITY_CHALLENGE", str(nav_result.challenge_type))
        return ApplicationRunResult(application_id, dice_job_id, StopReason.SECURITY_CHALLENGE, str(nav_result.challenge_type))
    if nav_result.already_applied:
        _fail(application_id, "FAILED", "ALREADY_APPLIED", "Dice reports this job as already applied")
        return ApplicationRunResult(application_id, dice_job_id, StopReason.ALREADY_APPLIED, "already applied")
    if not nav_result.easy_apply_visible:
        _fail(application_id, "FAILED", "STALE_INELIGIBLE", "Easy Apply is no longer available for this job")
        return ApplicationRunResult(application_id, dice_job_id, StopReason.STALE_OR_INELIGIBLE, "Easy Apply no longer available")
    add_event(application_id, event_type="job_opened", step="OPEN_JOB", message=canonical_url)

    open_result = open_easy_apply(page, nav_result)
    if not open_result.opened:
        _fail(application_id, "FAILED_RETRYABLE", "EASY_APPLY_OPEN_FAILED", open_result.reason)
        return ApplicationRunResult(application_id, dice_job_id, StopReason.EASY_APPLY_OPEN_FAILED, open_result.reason)
    add_event(application_id, event_type="easy_apply_opened", step="CLICK_EASY_APPLY", message=open_result.reason)

    if _proof_stop_after_easy_apply_open():
        # Live-proof-only hard stop: the wizard is open, nothing past this
        # point has run (no resume upload, no question fill, no Submit).
        # Deliberately does NOT call _fail() -- the application stays
        # PROCESSING (already set by the atomic claim), which is honest:
        # this run genuinely is still in progress, just paused for proof.
        return ApplicationRunResult(application_id, dice_job_id, StopReason.PROOF_STOP_EASY_APPLY_OPENED, open_result.reason)

    existing_resume = detect_existing_resume(page)
    if existing_resume is False:
        if resume_path is None:
            _fail(application_id, "FAILED_RETRYABLE", "RESUME_MISSING", "no resume on file and no resume_path configured")
            return ApplicationRunResult(application_id, dice_job_id, StopReason.RESUME_UPLOAD_FAILED, "no resume available")
        upload_result = upload_resume(page, resume_path)
        if not upload_result.uploaded:
            _fail(application_id, "FAILED_RETRYABLE", "RESUME_UPLOAD_FAILED", upload_result.reason)
            return ApplicationRunResult(application_id, dice_job_id, StopReason.RESUME_UPLOAD_FAILED, upload_result.reason)
        add_event(application_id, event_type="resume_uploaded", step="HANDLE_RESUME", message=upload_result.reason)

    if not is_review_screen(page):
        click_next(page)  # advance past the Resume step; harmless no-op if already elsewhere

    candidate = _load_candidate(candidate_id)

    blocker = _walk_questions_to_review(page, application_id, candidate_id, candidate, {})
    if blocker is not None:
        return ApplicationRunResult(application_id, dice_job_id, blocker.stop_reason, blocker.detail)

    return _gate_and_maybe_submit(page, application_id, dice_job_id, canonical_url, submission_policy)


def resume_needs_input_application(
    page: Page,
    application_id: str,
    worker_id: str,
    submission_policy: SubmissionPolicy = SubmissionPolicy.REQUIRE_CONFIRMATION,
    resume_path=None,
) -> ApplicationRunResult:
    """Re-opens the exact application, re-extracts questions, and fills
    each already-resolved intervention answer by its stable question_id.
    Dice's "Continue Application" restarts the wizard at Step 1 rather
    than resuming directly at Review (live-verified, 2026-08-21) -- this
    walks forward again rather than assuming any prior progress."""
    application = get_application(application_id)
    if application["status"] != "NEEDS_INPUT":
        return ApplicationRunResult(
            application_id, application["dice_job_id"], StopReason.NOT_YET_RESUMABLE,
            f"application status is {application['status']!r}, not NEEDS_INPUT",
        )

    readiness = compute_application_readiness(application_id)
    if readiness != ApplicationReadiness.RESUMABLE:
        return ApplicationRunResult(application_id, application["dice_job_id"], StopReason.NEEDS_INPUT, "not all interventions are resolved yet")

    dice_job_id = application["dice_job_id"]
    dice_job = get_dice_job(dice_job_id)
    canonical_url = dice_job["canonical_url"]

    nav_result = open_job(page, canonical_url)
    if not nav_result.authenticated:
        return ApplicationRunResult(application_id, dice_job_id, StopReason.AUTH_REQUIRED, "not authenticated")
    if nav_result.challenge_type is not None:
        return ApplicationRunResult(application_id, dice_job_id, StopReason.SECURITY_CHALLENGE, str(nav_result.challenge_type))
    if nav_result.already_applied:
        _fail(application_id, "FAILED", "ALREADY_APPLIED", "Dice reports this job as already applied")
        return ApplicationRunResult(application_id, dice_job_id, StopReason.ALREADY_APPLIED, "already applied")

    open_result = open_easy_apply(page, nav_result)
    if not open_result.opened:
        return ApplicationRunResult(application_id, dice_job_id, StopReason.EASY_APPLY_OPEN_FAILED, open_result.reason)

    existing_resume = detect_existing_resume(page)
    if existing_resume is False and resume_path is not None:
        upload_resume(page, resume_path)
    if not is_review_screen(page):
        click_next(page)

    update_application_status(application_id, "PROCESSING", worker_id=worker_id)

    candidate = _load_candidate(application["candidate_id"])
    resolved_overrides = get_resolved_answers(application_id)

    blocker = _walk_questions_to_review(page, application_id, application["candidate_id"], candidate, resolved_overrides)
    if blocker is not None:
        return ApplicationRunResult(application_id, dice_job_id, blocker.stop_reason, blocker.detail)

    return _gate_and_maybe_submit(page, application_id, dice_job_id, canonical_url, submission_policy)


def run_worker(
    page: Page,
    candidate_id: str,
    worker_id: str,
    max_applications: int = 1,
    submission_policy: SubmissionPolicy = SubmissionPolicy.REQUIRE_CONFIRMATION,
    resume_path=None,
    circuit_breaker_threshold: int = 3,
) -> WorkerRunSummary:
    """Sequential loop: one application fully processed (or stopped)
    before the next is claimed -- never concurrent. Halts (stops
    claiming more) after `circuit_breaker_threshold` consecutive
    session-level stops (AUTH_REQUIRED/SECURITY_CHALLENGE), rather than
    burning through every remaining slot hitting the identical wall."""
    summary = WorkerRunSummary()
    consecutive_session_stops = 0

    for _ in range(max_applications):
        result = process_one_application(page, candidate_id, worker_id, submission_policy, resume_path)
        summary.processed.append(result)

        if result.stop_reason == StopReason.NOTHING_QUEUED:
            break

        if result.stop_reason in _SESSION_LEVEL_STOPS:
            consecutive_session_stops += 1
        else:
            consecutive_session_stops = 0

        if consecutive_session_stops >= circuit_breaker_threshold:
            summary.halted = True
            summary.halt_reason = f"circuit breaker: {consecutive_session_stops} consecutive {result.stop_reason} stops"
            break

    return summary


def run_worker_for_run(
    page: Page,
    run_id: str,
    worker_id: str,
    submission_policy: SubmissionPolicy = SubmissionPolicy.REQUIRE_CONFIRMATION,
    resume_path=None,
) -> WorkerRunSummary:
    """Processes exactly the applications belonging to one bounded run
    (migration 20260822010000_application_runs.sql) -- claim_next_queued_
    application_for_run() only ever selects rows where applications.run_id
    matches, so this loop structurally cannot drift into an unrelated
    QUEUED application no matter what else exists for the same candidate.
    This is the guarantee behind the Jobs selection UI: "select 5 jobs"
    can never become "process every queued job".

    Halts immediately (not after run_worker()'s 3-strike circuit breaker)
    on a session-level stop (AUTH_REQUIRED/SECURITY_CHALLENGE) -- every
    job in one bounded run shares the same auth session, so if job 1
    can't authenticate, jobs 2-5 will fail identically; no point burning
    through the rest of the batch to find that out job by job.

    Checks run_registry.is_stopped() before claiming each next application,
    never mid-flight -- a Stop Run click can't interrupt an in-progress
    Submit/verification, only prevent the next one from starting."""
    run = run_registry.get_run(run_id)
    candidate_id = run["candidate_id"]
    run_registry.update_run_status(run_id, "RUNNING")

    summary = WorkerRunSummary()

    while True:
        if run_registry.is_stopped(run_id):
            summary.halted = True
            summary.halt_reason = "run stopped by operator"
            break

        result = process_one_application(
            page, candidate_id, worker_id, submission_policy, resume_path,
            claim_fn=lambda: claim_next_queued_application_for_run(run_id, worker_id),
        )
        summary.processed.append(result)

        if result.stop_reason == StopReason.NOTHING_QUEUED:
            break  # every application in this run has been processed

        if result.stop_reason in _SESSION_LEVEL_STOPS:
            summary.halted = True
            summary.halt_reason = f"session-level stop: {result.stop_reason.value}"
            break

    run_registry.update_run_status(run_id, "STOPPED" if summary.halted else "COMPLETE")
    return summary


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m dice_browser.worker",
        description=(
            "Standalone sequential Dice self-apply worker. Run this yourself, in your own "
            "terminal, against the already-authenticated dedicated Chrome (see dice_browser.session). "
            "Default submission policy is REQUIRE_CONFIRMATION: reaching Review never triggers Submit "
            "by itself."
        ),
    )
    parser.add_argument("--candidate-id", default=None, help="DicePilot candidate_id to process applications for (required unless --resume-application-id or --run-id is given)")
    parser.add_argument("--resume-application-id", default=None, help="Resume one specific NEEDS_INPUT application (calls resume_needs_input_application instead of the normal claim loop)")
    parser.add_argument("--run-id", default=None, help="Process exactly the application_ids recorded in this run_registry run (calls run_worker_for_run instead of the normal claim loop)")
    parser.add_argument("--cdp-url", default="http://127.0.0.1:9333", help="CDP endpoint of the already-running dedicated Chrome")
    parser.add_argument("--max-applications", type=int, default=1, help="Maximum applications to process this run (default 1)")
    parser.add_argument("--resume-path", default=None, help="Path to the resume file to upload if none is on file")
    parser.add_argument(
        "--submission-policy",
        choices=[p.value for p in SubmissionPolicy],
        default=SubmissionPolicy.REQUIRE_CONFIRMATION.value,
        help="REQUIRE_CONFIRMATION (default) stops at Review; AUTHORIZED_AUTONOMOUS also clicks Submit",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    if not args.resume_application_id and not args.run_id and not args.candidate_id:
        print("error: --candidate-id is required unless --resume-application-id or --run-id is given")
        return 2

    from playwright.sync_api import sync_playwright

    worker_id = f"worker-{uuid.uuid4()}"
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(args.cdp_url)
        ctx = browser.contexts[0]
        page = ctx.new_page()

        if args.resume_application_id:
            result = resume_needs_input_application(
                page,
                args.resume_application_id,
                worker_id,
                submission_policy=SubmissionPolicy(args.submission_policy),
                resume_path=args.resume_path,
            )
            print(f"{result.application_id or '-'}: {result.stop_reason.value} -- {result.detail}")
        elif args.run_id:
            summary = run_worker_for_run(
                page,
                args.run_id,
                worker_id,
                submission_policy=SubmissionPolicy(args.submission_policy),
                resume_path=args.resume_path,
            )
            for result in summary.processed:
                print(f"{result.application_id or '-'}: {result.stop_reason.value} -- {result.detail}")
            if summary.halted:
                print(f"HALTED: {summary.halt_reason}")
        else:
            summary = run_worker(
                page,
                candidate_id=args.candidate_id,
                worker_id=worker_id,
                max_applications=args.max_applications,
                submission_policy=SubmissionPolicy(args.submission_policy),
                resume_path=args.resume_path,
            )

            for result in summary.processed:
                print(f"{result.application_id or '-'}: {result.stop_reason.value} -- {result.detail}")
            if summary.halted:
                print(f"HALTED: {summary.halt_reason}")

        browser.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
