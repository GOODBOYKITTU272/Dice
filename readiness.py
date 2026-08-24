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
        return CheckResult(False, "Dice auth never verified for this candidate", Blocker.AUTH_REQUIRED)
    if not health["is_healthy"]:
        return CheckResult(False, health.get("invalidated_reason") or "Dice auth previously invalidated", Blocker.AUTH_REQUIRED)
    last_verified = health.get("last_verified_at")
    if not last_verified:
        return CheckResult(False, "Dice auth has no recorded verification timestamp", Blocker.AUTH_REQUIRED)
    verified_dt = datetime.fromisoformat(last_verified.replace("Z", "+00:00")) if isinstance(last_verified, str) else last_verified
    age_minutes = (datetime.now(timezone.utc) - verified_dt).total_seconds() / 60
    if age_minutes > _auth_health_ttl_minutes():
        return CheckResult(False, f"Dice auth verification stale ({age_minutes:.0f} min old)", Blocker.AUTH_REQUIRED)
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
