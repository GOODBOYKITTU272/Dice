"""Phase 8A: the single authoritative pre-offer readiness gate.

Core product rule this exists to enforce: if ApplyWizz asks a candidate
"Want me to apply?", the system must already have verified it is
currently capable of executing that application -- never send Apply/
Skip first and discover ten seconds later that Dice auth is dead,
Browserless is unreachable, the resume is missing, or the job was
already applied to.

This module only COMBINES existing capability into one decision --
every individual check reuses an existing, already-tested piece
(run_registry for worker health, dice_browser.browserless_session for
provider config, dice_auth_health_repository for auth history,
dice_browser.resume_delivery for resume existence, dice_jobs/
applications for job-specific eligibility/dedup). It never duplicates
their logic, and it never opens a browser or spends Browserless minutes
just to answer a readiness question -- see check_browser_provider_ready
and check_dice_auth_ready's own docstrings for the specific cost
tradeoffs that decision makes.

Deliberately NOT wired into anything yet (Phase 8A only) -- no
messaging changes, no job-offer creation changes. evaluate_offer_
readiness() is a pure query: given a candidate and a job, is this
OFFERABLE right now, and if not, why not.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

import run_registry
from db import dice_auth_health_repository
from db.supabase_client import get_supabase_client
from dice_browser.browser_provider import VALID_PROVIDERS, resolve_browser_provider
from dice_browser.resume_delivery import resume_exists_in_storage

# Reasoned default, not arbitrary: tonight's live finding was that a
# real Dice access token's own short-lived `exp` is routinely hours
# away from the actual session ceiling (`inactivity_exp`) -- a
# positively-verified-healthy signal is very unlikely to have silently
# gone bad within half an hour. Long enough that surfacing a handful of
# offers doesn't each force a fresh live Dice check (the real cost this
# cache exists to avoid); short enough that a genuine session death
# doesn't stay invisible for hours. Configurable because "unlikely to
# go bad" is a judgment call, not a guarantee.
DEFAULT_AUTH_HEALTH_TTL_MINUTES = 30
_AUTH_HEALTH_TTL_ENV_VAR = "DICEPILOT_AUTH_HEALTH_TTL_MINUTES"

_SYNTHETIC_JOB_PREFIXES = ("SYNTHETIC-", "TEST-")
_ACTIVE_APPLICATION_STATUSES = ("AWAITING_USER_DECISION", "QUEUED", "PROCESSING", "SUBMITTING", "NEEDS_INPUT")


class Blocker(str, Enum):
    WORKER_UNAVAILABLE = "WORKER_UNAVAILABLE"
    BROWSER_UNAVAILABLE = "BROWSER_UNAVAILABLE"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    AUTH_NEVER_VERIFIED = "AUTH_NEVER_VERIFIED"
    AUTH_HEALTH_STALE = "AUTH_HEALTH_STALE"
    RESUME_MISSING = "RESUME_MISSING"
    CANDIDATE_CONFIG_INVALID = "CANDIDATE_CONFIG_INVALID"
    JOB_CLOSED = "JOB_CLOSED"
    NOT_EASY_APPLY = "NOT_EASY_APPLY"
    ALREADY_APPLIED = "ALREADY_APPLIED"
    NOT_ELIGIBLE = "NOT_ELIGIBLE"
    DUPLICATE_APPLICATION = "DUPLICATE_APPLICATION"


@dataclass
class CheckResult:
    ready: bool
    reason: str | None = None
    blocker: Blocker | None = None


@dataclass
class ReadinessResult:
    offerable: bool
    worker: CheckResult
    browser: CheckResult
    dice_auth: CheckResult
    resume: CheckResult
    candidate: CheckResult
    job: CheckResult
    blocker: Blocker | None = None


def check_worker_ready() -> CheckResult:
    status = run_registry.worker_status()
    if status["online"]:
        return CheckResult(True)
    return CheckResult(False, "worker offline or heartbeat stale", Blocker.WORKER_UNAVAILABLE)


def check_browser_provider_ready() -> CheckResult:
    """Cheap config-presence check only -- deliberately never creates a
    real browser/session just to answer "is the provider configured".
    This proves "configured", not "reachable this exact millisecond";
    a real connect attempt happens naturally when the worker actually
    claims the run, so this is a conscious cost/latency tradeoff (per
    the product spec's own cost-control requirement), not a
    correctness gap -- a provider that's configured but transiently
    down is still caught by the post-Apply execution-time recheck."""
    provider = resolve_browser_provider()
    if provider not in VALID_PROVIDERS:
        return CheckResult(False, f"unrecognized browser provider {provider!r}", Blocker.BROWSER_UNAVAILABLE)
    if provider == "browserless":
        from dice_browser.browserless_session import is_configured

        if not is_configured():
            return CheckResult(False, "Browserless not configured", Blocker.BROWSER_UNAVAILABLE)
    return CheckResult(True)


def _auth_health_ttl_minutes() -> int:
    raw = os.environ.get(_AUTH_HEALTH_TTL_ENV_VAR)
    if raw is None:
        return DEFAULT_AUTH_HEALTH_TTL_MINUTES
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_AUTH_HEALTH_TTL_MINUTES


def check_dice_auth_ready(candidate_id: str) -> CheckResult:
    """Never blindly trusts that cookies exist -- reads the durable
    dice_auth_health signal instead (db.dice_auth_health_repository).
    A known invalidation (is_healthy=False, e.g. from a real
    AUTH_REQUIRED anywhere) always blocks regardless of TTL -- that's
    the one thing this cache must never paper over. A positive result
    is only trusted within DEFAULT_AUTH_HEALTH_TTL_MINUTES of its own
    last_verified_at. No record at all is treated as not-yet-verified,
    never optimistically healthy."""
    health = dice_auth_health_repository.get_auth_health(candidate_id)
    if health is None:
        return CheckResult(False, "Dice auth never verified for this candidate", Blocker.AUTH_NEVER_VERIFIED)
    if not health["is_healthy"]:
        return CheckResult(False, health.get("invalidated_reason") or "Dice auth previously invalidated", Blocker.AUTH_REQUIRED)
    last_verified = health.get("last_verified_at")
    if not last_verified:
        return CheckResult(False, "Dice auth has no recorded verification timestamp", Blocker.AUTH_NEVER_VERIFIED)
    verified_dt = datetime.fromisoformat(last_verified.replace("Z", "+00:00")) if isinstance(last_verified, str) else last_verified
    age_minutes = (datetime.now(timezone.utc) - verified_dt).total_seconds() / 60
    if age_minutes > _auth_health_ttl_minutes():
        return CheckResult(False, f"Dice auth verification stale ({age_minutes:.0f} min old)", Blocker.AUTH_HEALTH_STALE)
    return CheckResult(True)


def check_resume_ready(candidate_id: str) -> CheckResult:
    if resume_exists_in_storage(candidate_id):
        return CheckResult(True)
    return CheckResult(False, "no usable resume on file", Blocker.RESUME_MISSING)


def check_candidate_ready(candidate_id: str) -> CheckResult:
    """Matches worker_daemon.check_startup_readiness's own existing
    convention: a configured candidate_id is sufficient here, not a
    live upstream profile fetch (APPLYWIZZ_API_BASE_URL isn't
    configured in this environment, and unknown application questions
    are explicitly out of scope for this gate -- NEEDS_INPUT handles
    those after Apply, not before the offer)."""
    if candidate_id and isinstance(candidate_id, str):
        return CheckResult(True)
    return CheckResult(False, "no candidate configured", Blocker.CANDIDATE_CONFIG_INVALID)


def check_job_ready(candidate_id: str, dice_job_id: str) -> CheckResult:
    """Cheap, DB-only -- never opens a browser, never walks the Easy
    Apply wizard. dice_job_id is dice_jobs.id (applications.dice_job_id's
    own foreign key), matching db.application_repository's existing
    convention throughout this project."""
    client = get_supabase_client()
    rows = client.table("dice_jobs").select("*").eq("id", dice_job_id).execute().data
    if not rows:
        return CheckResult(False, "job not found", Blocker.JOB_CLOSED)
    job = rows[0]

    external_id = job.get("dice_job_id") or ""
    if external_id.startswith(_SYNTHETIC_JOB_PREFIXES):
        return CheckResult(False, "synthetic/test job, never offerable", Blocker.NOT_ELIGIBLE)
    if job.get("c2c_status") not in ("CONFIRMED", "LIKELY"):
        return CheckResult(False, "not C2C/contract eligible", Blocker.NOT_ELIGIBLE)
    if not job.get("is_easy_apply"):
        return CheckResult(False, "not Easy Apply", Blocker.NOT_EASY_APPLY)

    existing = (
        client.table("applications")
        .select("status")
        .eq("candidate_id", candidate_id)
        .eq("dice_job_id", dice_job_id)
        .execute()
        .data
    )
    if existing:
        status = existing[0]["status"]
        if status == "SUBMITTED":
            return CheckResult(False, "already applied", Blocker.ALREADY_APPLIED)
        if status == "SKIPPED":
            return CheckResult(False, "previously skipped", Blocker.NOT_ELIGIBLE)
        if status in _ACTIVE_APPLICATION_STATUSES:
            return CheckResult(False, "application already active", Blocker.DUPLICATE_APPLICATION)
        # FAILED / FAILED_RETRYABLE: a prior attempt exists but isn't
        # active or submitted. Re-offering is a separate, deliberate
        # decision (db.application_repository.requeue_failed_application),
        # never automatic just because this gate ran again.
        return CheckResult(False, "prior application exists and is not active/submitted", Blocker.DUPLICATE_APPLICATION)

    return CheckResult(True)


def evaluate_offer_readiness(candidate_id: str, dice_job_id: str) -> ReadinessResult:
    """The one function everything else calls. Checks run in the order
    the product spec defines (worker -> browser -> dice auth -> resume
    -> candidate -> job) and the FIRST failing check's blocker is what
    gets surfaced -- never a generic combined failure, matching "do not
    collapse everything into generic FAILED"."""
    worker = check_worker_ready()
    browser = check_browser_provider_ready()
    dice_auth = check_dice_auth_ready(candidate_id)
    resume = check_resume_ready(candidate_id)
    candidate = check_candidate_ready(candidate_id)
    job = check_job_ready(candidate_id, dice_job_id)

    checks = (worker, browser, dice_auth, resume, candidate, job)
    offerable = all(c.ready for c in checks)
    blocker = next((c.blocker for c in checks if not c.ready), None)

    return ReadinessResult(
        offerable=offerable,
        worker=worker,
        browser=browser,
        dice_auth=dice_auth,
        resume=resume,
        candidate=candidate,
        job=job,
        blocker=blocker,
    )


def offer_job_if_ready(provider, candidate_id: str, dice_job_id: str) -> dict:
    """Phase 8C: THE single production entrypoint for putting a
    candidate's job in front of "Want me to apply?" -- every real path
    that can create an AWAITING_USER_DECISION application and send a
    Telegram/iMessage offer card must go through this, never call
    db.application_repository.create_job_offer() +
    attention.service.notify_job_offer() directly outside it (tests/
    debug tooling may still call those directly, explicitly, never a
    production path).

    Held opportunities need no new schema/state: a NOT_OFFERABLE result
    simply never creates an application row at all, so the job is left
    exactly as reconsiderable next run as it was before -- check_job_
    ready()'s own dedup logic is what makes that safe (a job with no
    application row is, definitionally, still eligible; nothing here
    needs to remember "this was held" separately).

    Phase M8C: AUTH_HEALTH_STALE, and ONLY that blocker (every other
    check already passed), is not treated as a dead end -- see
    _offer_after_auth_recovery. Every other blocker still just reports
    and holds, unchanged.

    Reported gap, not silently worked around: there is nowhere durable
    to log WHY a specific job was blocked when no application row ever
    gets created for it (application_events is keyed on application_id,
    which doesn't exist yet for a blocked offer) -- the smallest fix
    would be a new event log keyed on (candidate_id, dice_job_id)
    instead, not built here to avoid inventing new workflow machinery
    for what's currently only ever called from an ad-hoc script."""
    result = evaluate_offer_readiness(candidate_id, dice_job_id)
    if not result.offerable:
        if _is_auth_stale_the_only_blocker(result):
            return _offer_after_auth_recovery(provider, candidate_id, dice_job_id, result)
        return {"offered": False, "blocker": result.blocker.value if result.blocker else None, "readiness": result}

    return _create_and_send_offer(provider, candidate_id, dice_job_id, result)


def _create_and_send_offer(provider, candidate_id: str, dice_job_id: str, result: ReadinessResult) -> dict:
    from attention.service import notify_job_offer
    from db.application_repository import DuplicateApplicationError, add_event, create_job_offer

    try:
        application = create_job_offer(candidate_id, dice_job_id)
    except DuplicateApplicationError:
        # Real TOCTOU race: readiness's own dedup check ran moments
        # before this insert -- another concurrent offer already won.
        # Not an error the caller should crash on; just no new offer.
        return {"offered": False, "blocker": Blocker.DUPLICATE_APPLICATION.value, "readiness": result}

    add_event(application["id"], event_type="readiness_check", step="READINESS_OFFERABLE", message="all pre-offer checks passed")
    notify_job_offer(provider, application["id"])
    return {"offered": True, "application_id": application["id"], "readiness": result}


def _is_auth_stale_the_only_blocker(result: ReadinessResult) -> bool:
    """Guards the auto-recovery path so it only ever fires for the exact
    case the product spec calls for: a genuinely eligible opportunity
    (every OTHER check already passed) blocked purely on an expired
    freshness timestamp -- never spending a Browserless session on a job
    that has some other, unrelated problem too."""
    return (
        result.dice_auth.blocker == Blocker.AUTH_HEALTH_STALE
        and result.worker.ready
        and result.browser.ready
        and result.resume.ready
        and result.candidate.ready
        and result.job.ready
    )


_auth_verification_locks: dict[str, threading.Lock] = {}
_auth_verification_locks_guard = threading.Lock()


def _try_acquire_auth_verification_lock(candidate_id: str) -> bool:
    """Candidate A gets at most one active auth-verification attempt at
    a time -- a second stale-eligible job for the same candidate arriving
    while one is already in flight finds the lock held and is left held,
    never triggering a second Browserless session."""
    with _auth_verification_locks_guard:
        lock = _auth_verification_locks.setdefault(candidate_id, threading.Lock())
    return lock.acquire(blocking=False)


def _release_auth_verification_lock(candidate_id: str) -> None:
    with _auth_verification_locks_guard:
        lock = _auth_verification_locks.get(candidate_id)
    if lock is not None:
        lock.release()


def _offer_after_auth_recovery(provider, candidate_id: str, dice_job_id: str, stale_result: ReadinessResult) -> dict:
    """AUTH_HEALTH_STALE alone (every other check already passed) means
    the durable freshness timestamp expired, not that the login is
    necessarily dead -- a human being asked "please manually approve a
    read-only login check" every time that timestamp ages past
    DEFAULT_AUTH_HEALTH_TTL_MINUTES is exactly the product gap this
    closes. Runs ONE bounded, read-only verification (reconnect_dice's
    own canonical path -- navigator.open_job, its reload-retry included
    -- unchanged, not duplicated here) and re-evaluates the SAME job
    once, fresh. Never creates an application or sends Apply/Skip before
    the verification result and the fresh re-check are both known.

    If verification itself finds AUTH_REQUIRED, or the job is no longer
    offerable for some other reason after the fresh re-check, the job is
    simply left held -- no application row, exactly as reconsiderable on
    a later call as any other blocked opportunity. Never a second offer:
    the fresh re-check goes through the exact same create_job_offer()
    dedup path as every other offer."""
    if not _try_acquire_auth_verification_lock(candidate_id):
        return {"offered": False, "blocker": Blocker.AUTH_HEALTH_STALE.value, "readiness": stale_result}
    try:
        try:
            reconnect_result = reconnect_dice(provider, candidate_id)
        except Exception:
            # Browserless/provider itself unreachable -- distinct from a
            # confirmed AUTH_REQUIRED: the login status is still unknown,
            # not disproven, so this stays under the SAME stale blocker
            # (still reconsiderable) rather than escalating to "reconnect
            # required", which would incorrectly claim the login is dead.
            return {"offered": False, "blocker": Blocker.AUTH_HEALTH_STALE.value, "readiness": stale_result}
    finally:
        _release_auth_verification_lock(candidate_id)

    if not reconnect_result.get("reconnected"):
        return {"offered": False, "blocker": Blocker.AUTH_REQUIRED.value, "readiness": stale_result}

    fresh_result = evaluate_offer_readiness(candidate_id, dice_job_id)
    if not fresh_result.offerable:
        return {"offered": False, "blocker": fresh_result.blocker.value if fresh_result.blocker else None, "readiness": fresh_result}

    return _create_and_send_offer(provider, candidate_id, dice_job_id, fresh_result)


def reconnect_dice(provider, candidate_id: str) -> dict:
    """Phase 8D: the manual-trigger stand-in for the real interactive
    "Reconnect Dice" flow. Deferred, not skipped: Browserless's LiveURL
    (the feature that would let a candidate log into Dice directly and
    have us positively verify it) requires a paid plan we don't have,
    confirmed live 2026-08-25 ("Your plan does not support Live URLs").
    Swappable by design -- whatever eventually triggers a real
    interactive login success (a paid LiveURL flow, or anything else)
    should call this exact function, unchanged, as its completion step;
    right now the trigger is simply "an operator provisioned this
    candidate's auth state (db.dice_auth_state_repository.save_auth_
    state) and asks us to check."

    Never trusts "the login page disappeared" -- runs one short-lived,
    bounded Browserless session through the SAME canonical auth-
    classification path (navigator.open_job, its own reload-retry
    included) real application processing uses, and only records what
    that positively observed. The session is a few minutes, never held
    open (cost control).

    Phase M8B: reads this candidate's own auth state (db.dice_auth_
    state_repository, Vault-backed) -- never the old global
    DICE_AUTH_COOKIES_JSON. An operator provisions/replaces a candidate's
    state with db.dice_auth_state_repository.save_auth_state(candidate_id,
    cookies_json) before calling this; this function only ever verifies
    and records health for the ONE candidate_id it's given."""
    from playwright.sync_api import sync_playwright

    from dice_browser.browserless_session import create_session, load_dice_cookies_for_candidate, stop_session, to_playwright_cookies
    from dice_browser.navigator import open_job
    from dice_browser.worker import _record_auth_health

    raw_cookies = load_dice_cookies_for_candidate(candidate_id)
    if not raw_cookies:
        return {"reconnected": False, "reason": "no candidate-scoped Dice auth state configured for this candidate"}

    client = get_supabase_client()
    any_job = client.table("dice_jobs").select("canonical_url").limit(1).execute().data
    if not any_job:
        return {"reconnected": False, "reason": "no known Dice job to verify against"}
    canonical_url = any_job[0]["canonical_url"]

    session = create_session(ttl_ms=180000, process_keep_alive_ms=60000)
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(session["connect"])
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            context.add_cookies(to_playwright_cookies(raw_cookies))
            page = context.new_page()
            nav_result = open_job(page, canonical_url)
            _record_auth_health(candidate_id, nav_result)
            browser.close()
    finally:
        stop_session(session.get("stop"))

    if not nav_result.authenticated:
        return {"reconnected": False, "reason": nav_result.evidence}

    resumed = _resume_interrupted_applications(provider, candidate_id)
    return {"reconnected": True, "resumed_application_ids": resumed}


def _resume_interrupted_applications(provider, candidate_id: str) -> list[str]:
    """Distinguishes already-authorized, interrupted applications
    (FAILED_RETRYABLE with error_code=AUTH_REQUIRED -- the candidate
    already tapped Apply) from held, not-yet-offered opportunities
    (AWAITING_USER_DECISION, blocked pre-offer by readiness -- the
    candidate was never asked). Only the former are auto-resumed here;
    re-offering a held opportunity is a separate, deliberate
    offer_job_if_ready() call per job (spec: "avoid blasting every held
    opportunity simultaneously").

    requeue_failed_application() alone only flips status -- the worker
    claims RUNS, not raw QUEUED applications (a real gap found earlier
    tonight) -- so a fresh run is always created too, preserving the
    ORIGINAL run's submission_policy rather than assuming one, since
    resuming must never grant a different authorization than the
    candidate's original Apply tap did."""
    from db.application_repository import requeue_failed_application
    from attention.service import notify_reconnect_success

    client = get_supabase_client()
    stuck = (
        client.table("applications")
        .select("id, run_id")
        .eq("candidate_id", candidate_id)
        .eq("status", "FAILED_RETRYABLE")
        .eq("error_code", "AUTH_REQUIRED")
        .execute()
        .data
    )
    resumed = []
    for row in stuck:
        submission_policy = "AUTHORIZED_AUTONOMOUS"
        if row.get("run_id"):
            old_run = run_registry.get_run(row["run_id"])
            if old_run and old_run.get("submission_policy"):
                submission_policy = old_run["submission_policy"]

        requeue_failed_application(row["id"])
        run_registry.create_run([row["id"]], candidate_id, submission_policy=submission_policy)
        try:
            notify_reconnect_success(provider, row["id"])
        except Exception:
            pass
        resumed.append(row["id"])
    return resumed
