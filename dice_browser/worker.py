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
import sys
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from playwright.sync_api import Page

import run_registry
from db.application_repository import (
    add_event,
    claim_next_queued_application,
    get_application,
    get_dice_job,
    update_application_status,
)
from db.intervention_repository import (
    ApplicationReadiness,
    compute_application_readiness,
    create_or_get_question_intervention,
    get_resolved_answers,
)
from db.submission_repository import record_submission_result
from dice.answer_resolution import resolve_safe_answer
from dice.candidate_adapter import fetch_candidate
from dice.models import CandidateFetchStatus, CandidateProfile
from dice_browser.easy_apply import open_easy_apply
from dice_browser.models import FieldType, QuestionExtractionStatus, QuestionStatus, SubmissionStatus
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


def _load_candidate(candidate_id: str) -> CandidateProfile | None:
    result = fetch_candidate(candidate_id)
    return result.profile if result.status == CandidateFetchStatus.SUCCESS else None


def _walk_questions_to_review(
    page: Page,
    application_id: str,
    candidate: CandidateProfile | None,
    resolved_overrides: dict[str, Any],
) -> ApplicationRunResult | None:
    """Advances through question step(s) until Review is reached or a
    blocker stops progress. Returns None on success (Review reached);
    an ApplicationRunResult if something stopped it. `resolved_overrides`
    are question_id -> answer pairs already resolved via a Supabase
    intervention (the resume path) -- these are filled directly, never
    re-asked."""
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
        return ApplicationRunResult(application_id, dice_job_id, StopReason.AWAITING_SUBMIT_CONFIRMATION, "awaiting human confirmation")

    return _submit_with_verification(page, application_id, dice_job_id, canonical_url)


def _submit_with_verification(page: Page, application_id: str, dice_job_id: str, canonical_url: str) -> ApplicationRunResult:
    """AUTHORIZED_AUTONOMOUS path -- architected, not exercised live as
    of Phase 6's creation. Never retries: whatever submit_application()
    classifies is recorded once and this function returns."""
    update_application_status(application_id, "SUBMITTING")
    preconditions = SubmitPreconditions(authenticated=True, no_unresolved_interventions=True, already_verified_submitted=False)
    result = submit_application(page, canonical_url, application_id, dice_job_id, preconditions)
    record_submission_result(application_id, result)

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

    open_result = open_easy_apply(page, nav_result)
    if not open_result.opened:
        _fail(application_id, "FAILED_RETRYABLE", "EASY_APPLY_OPEN_FAILED", open_result.reason)
        return ApplicationRunResult(application_id, dice_job_id, StopReason.EASY_APPLY_OPEN_FAILED, open_result.reason)
    add_event(application_id, event_type="easy_apply_opened", step="CLICK_EASY_APPLY", message=open_result.reason)

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

    blocker = _walk_questions_to_review(page, application_id, candidate, {})
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

    blocker = _walk_questions_to_review(page, application_id, candidate, resolved_overrides)
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


def _claim_specific_application(application_id: str, worker_id: str) -> dict[str, Any] | None:
    """Non-atomic equivalent of claim_next_queued_application(), scoped to
    one already-known id instead of a pool query -- acceptable here (no
    new race introduced) because this project is single-worker V1 by
    design (see this module's own docstring), and a bounded run is
    processed by exactly one worker process at a time. Returns None if
    the application isn't QUEUED anymore (already terminal from a prior
    run, or otherwise moved on) -- the caller treats that as "nothing to
    do for this id", never as a reason to re-process or re-submit it."""
    application = get_application(application_id)
    if application["status"] != "QUEUED":
        return None
    return update_application_status(application_id, "PROCESSING", worker_id=worker_id, started_at=application.get("started_at"))


def run_worker_for_run(
    page: Page,
    run_id: str,
    worker_id: str,
    submission_policy: SubmissionPolicy = SubmissionPolicy.REQUIRE_CONFIRMATION,
    resume_path=None,
) -> WorkerRunSummary:
    """Processes exactly the application_ids recorded in run_registry's
    run -- never a broader DB pool query -- one at a time, in the order
    selected. This is the critical guarantee behind the Jobs selection UI:
    "select 5 jobs" can never become "process every queued job", because
    this loop never asks the database "what's next", only "process this
    specific id, then that one."

    Halts immediately (not after run_worker()'s 3-strike circuit breaker)
    on a session-level stop (AUTH_REQUIRED/SECURITY_CHALLENGE) -- every
    job in one bounded run shares the same auth session, so if job 1
    can't authenticate, jobs 2-5 will fail identically; no point burning
    through the rest of the batch to find that out job by job.

    Checks run_registry.is_stopped() before claiming each next id, never
    mid-flight -- a Stop Run click can't interrupt an in-progress
    Submit/verification, only prevent the next one from starting."""
    run = run_registry.get_run(run_id)
    candidate_id = run["candidate_id"]
    run_registry.update_run_status(run_id, "RUNNING")

    summary = WorkerRunSummary()

    for application_id in run["application_ids"]:
        if run_registry.is_stopped(run_id):
            summary.halted = True
            summary.halt_reason = "run stopped by operator"
            break

        def claim_fn(aid: str = application_id) -> dict[str, Any] | None:
            return _claim_specific_application(aid, worker_id)

        result = process_one_application(page, candidate_id, worker_id, submission_policy, resume_path, claim_fn=claim_fn)
        summary.processed.append(result)

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
