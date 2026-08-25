"""Phase 6.3: bounded-run identity, now with a real poll/claim split
between the Vercel-deployed frontend and the standalone Mac worker
daemon (dice_browser.worker_daemon).

Backed by Supabase (application_runs table, applications.run_id column,
worker_heartbeats table -- migrations 20260822010000_application_runs.sql
and 20260822020000_worker_daemon.sql). Runs are written PENDING by
whichever Flask process handles "Start Applications" (local or Vercel --
same code, and neither one launches a worker process); claim_next_pending_run()
is the only thing that ever transitions a run to RUNNING, and only the
worker daemon calls it.

stop_requested is deliberately separate from status: the "Stop Run"
button sets the flag, and the worker daemon (already mid-loop, inside
run_worker_for_run()) is the only writer of status -> STOPPED, so an
operator's stop request can never race with the daemon's own status
writes on a run it's actively processing.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from db.supabase_client import get_supabase_client


class RunNotFoundError(RuntimeError):
    pass


def create_run(application_ids: list[str], candidate_id: str, submission_policy: str = "AUTHORIZED_AUTONOMOUS") -> dict[str, Any]:
    """One run per "Start Applications" click. Inserts the run row
    PENDING, then stamps run_id onto exactly the given (already-enqueued)
    application rows -- callers enqueue first, then create_run().

    submission_policy is resolved and persisted onto the run at creation
    time -- the daemon reads it per-run (never a CLI-wide default), and
    it never changes after creation (a later Settings change or a Resume
    Run click cannot retroactively alter an already-created run's
    policy)."""
    client = get_supabase_client()
    run_row = (
        client.table("application_runs")
        .insert({"candidate_id": candidate_id, "status": "PENDING", "submission_policy": submission_policy})
        .execute()
        .data[0]
    )
    if application_ids:
        client.table("applications").update({"run_id": run_row["id"]}).in_("id", application_ids).execute()
    return _to_run_dict(run_row, application_ids)


def get_run(run_id: str) -> dict[str, Any]:
    client = get_supabase_client()
    rows = client.table("application_runs").select("*").eq("id", run_id).execute().data
    if not rows:
        raise RunNotFoundError(run_id)
    application_ids = _application_ids_for_run(client, run_id)
    return _to_run_dict(rows[0], application_ids)


def claim_next_pending_run(worker_id: str, candidate_id: str) -> dict[str, Any] | None:
    """Atomic: claims the oldest PENDING run for THIS candidate (FOR
    UPDATE SKIP LOCKED, server-side), stamping claimed_by/claimed_at and
    moving it to RUNNING. Returns None when nothing is PENDING -- the
    daemon's normal "nothing to do yet" case, not an error.

    Real gap, live-found 2026-08-25: the RPC previously took no
    candidate_id at all and claimed the globally-oldest PENDING run
    regardless of owner -- masked in V1 (one real candidate in
    practice), but it meant the real production worker was silently
    claiming test-created runs mid-suite. Scoped the same way
    claim_next_queued_application() already is."""
    client = get_supabase_client()
    rows = client.rpc("claim_next_pending_run", {"p_worker_id": worker_id, "p_candidate_id": candidate_id}).execute().data
    if not rows:
        return None
    application_ids = _application_ids_for_run(client, rows[0]["id"])
    return _to_run_dict(rows[0], application_ids)


def update_run_status(run_id: str, status: str) -> dict[str, Any]:
    client = get_supabase_client()
    result = client.table("application_runs").update({"status": status}).eq("id", run_id).execute()
    if not result.data:
        raise RunNotFoundError(run_id)
    return get_run(run_id)


class InvalidResumeError(RuntimeError):
    """Raised when resume_run() is called on a run that isn't STOPPED --
    resuming a RUNNING/PENDING run would be a no-op at best and resuming
    a COMPLETE one would silently reopen a finished batch."""


def resume_run(run_id: str) -> dict[str, Any]:
    """The UI counterpart to request_stop(): moves a deliberately-STOPPED
    run back to PENDING so the daemon's normal poll loop claims it again
    on its own. Only valid from STOPPED -- this is not a generic status
    setter."""
    client = get_supabase_client()
    current = get_run(run_id)
    if current["status"] != "STOPPED":
        raise InvalidResumeError(f"run {run_id} is {current['status']!r}, not STOPPED")
    result = (
        client.table("application_runs")
        .update({"status": "PENDING", "stop_requested": False, "claimed_by": None, "claimed_at": None})
        .eq("id", run_id)
        .execute()
    )
    if not result.data:
        raise RunNotFoundError(run_id)
    return get_run(run_id)


def request_stop(run_id: str) -> dict[str, Any]:
    """Sets stop_requested -- does not touch status. The worker daemon
    checks is_stopped() before claiming its next application and, if
    set, is the one that actually writes status -> STOPPED."""
    client = get_supabase_client()
    result = client.table("application_runs").update({"stop_requested": True}).eq("id", run_id).execute()
    if not result.data:
        raise RunNotFoundError(run_id)
    return get_run(run_id)


def is_stopped(run_id: str) -> bool:
    """Never raises -- a run that can't be found isn't a "stopped" run,
    it's an absent one; callers checking this mid-loop shouldn't crash
    on a lookup miss."""
    try:
        return bool(get_run(run_id)["stop_requested"])
    except RunNotFoundError:
        return False


def write_heartbeat(worker_id: str, status: str = "ONLINE") -> dict[str, Any]:
    client = get_supabase_client()
    now = datetime.now(timezone.utc).isoformat()
    return (
        client.table("worker_heartbeats")
        .upsert({"worker_id": worker_id, "status": status, "last_heartbeat_at": now}, on_conflict="worker_id")
        .execute()
        .data[0]
    )


def get_latest_heartbeat(worker_id: str | None = None) -> dict[str, Any] | None:
    """worker_id, when given, scopes to that one worker's own row
    instead of the globally most-recent heartbeat across every worker
    that's ever run. Production (V1, one real worker) never passes this
    -- "global latest" is the correct, intended semantic there. Tests
    pass their own synthetic worker_id so the real production worker's
    genuinely-fresher heartbeat can never mask what a test is actually
    trying to verify (a real gap, live-found 2026-08-25: this exact
    unscoped query made every heartbeat-staleness test nondeterministic
    once a real worker was continuously running)."""
    client = get_supabase_client()
    query = client.table("worker_heartbeats").select("*")
    if worker_id is not None:
        query = query.eq("worker_id", worker_id)
    rows = query.order("last_heartbeat_at", desc=True).limit(1).execute().data
    return rows[0] if rows else None


def worker_status(max_age_seconds: int = 30, worker_id: str | None = None) -> dict[str, Any]:
    """Used by the Run Progress and Worker pages. Never assumes a worker
    process is alive just because the web app itself is reachable --
    "online" means the daemon PROCESS is up and heartbeating, nothing
    more; it stays True even when the browser/Dice session has a
    problem, because a fresh AUTH_REQUIRED heartbeat still proves the
    daemon itself is alive and honestly reporting that problem (as
    opposed to a stale/absent heartbeat, which means the process itself
    is gone). status carries the real, specific state -- ONLINE,
    BROWSER_DISCONNECTED, AUTH_REQUIRED, or SECURITY_CHALLENGE when
    online, else the synthetic OFFLINE when the heartbeat itself has
    gone stale or never existed -- so the frontend can show "Dice Login
    Required" or "Security Challenge" distinctly from a dead worker.

    worker_id: see get_latest_heartbeat() -- test-only scoping, never
    passed by real production callers."""
    hb = get_latest_heartbeat(worker_id)
    if hb is None:
        return {"online": False, "status": "OFFLINE", "last_heartbeat_at": None, "age_seconds": None}
    last_raw = hb["last_heartbeat_at"]
    last_dt = datetime.fromisoformat(last_raw.replace("Z", "+00:00")) if isinstance(last_raw, str) else last_raw
    age_seconds = (datetime.now(timezone.utc) - last_dt).total_seconds()
    online = age_seconds <= max_age_seconds
    return {
        "online": online,
        "status": hb["status"] if online else "OFFLINE",
        "last_heartbeat_at": last_raw,
        "age_seconds": age_seconds,
    }


DEFAULT_STALE_LEASE_SECONDS = 90


def _worker_heartbeat_is_stale(client, worker_id: str | None, max_age_seconds: int) -> bool:
    """The lease signal is the CLAIMING WORKER's heartbeat freshness, not
    claimed_at's raw age -- claimed_at is set once, at claim time, and
    never touched again while a run is legitimately still being worked
    through a long batch, so age-since-claim alone would misclassify
    slow-but-alive work as orphaned. A worker whose heartbeat has gone
    stale is the actual "crashed" signal."""
    if not worker_id:
        return True
    rows = client.table("worker_heartbeats").select("last_heartbeat_at").eq("worker_id", worker_id).execute().data
    if not rows:
        return True
    last_raw = rows[0]["last_heartbeat_at"]
    last_dt = datetime.fromisoformat(last_raw.replace("Z", "+00:00")) if isinstance(last_raw, str) else last_raw
    age_seconds = (datetime.now(timezone.utc) - last_dt).total_seconds()
    return age_seconds > max_age_seconds


def recover_stale_applications(max_heartbeat_age_seconds: int = DEFAULT_STALE_LEASE_SECONDS) -> dict[str, list[str]]:
    """Startup reconciliation for applications left behind by a worker
    whose heartbeat has gone stale (crashed mid-application).

    PROCESSING is entirely pre-Submit -- safe to hand straight back to
    QUEUED so the next daemon poll can pick it up fresh (bypasses
    STATUS_TRANSITIONS's normal map on purpose: QUEUED is deliberately
    not a reachable target from PROCESSING through the normal API,
    since normal code should never "un-claim" an application anyone is
    actively working -- this is the one narrow, explicit exception,
    gated on independently-verified worker death, not a general escape
    hatch).

    SUBMITTING crossed into the actual Submit attempt -- whether the
    click landed on a since-crashed worker is genuinely unknown, so this
    NEVER re-queues it automatically (that would risk a second real
    Submit click against Dice). It lands on FAILED_RETRYABLE with an
    explicit reason, inert until a human verifies on Dice's own Applied
    Jobs page and deliberately calls requeue_failed_application()
    themselves -- this is the "require verification/reconciliation
    first" the crash-recovery design calls for."""
    client = get_supabase_client()
    recovered: dict[str, list[str]] = {"requeued": [], "needs_verification": []}

    processing = client.table("applications").select("id, worker_id").eq("status", "PROCESSING").execute().data
    for application in processing:
        if _worker_heartbeat_is_stale(client, application.get("worker_id"), max_heartbeat_age_seconds):
            client.table("applications").update({"status": "QUEUED", "worker_id": None, "lock_acquired_at": None}).eq(
                "id", application["id"]
            ).execute()
            recovered["requeued"].append(application["id"])

    submitting = client.table("applications").select("id, worker_id").eq("status", "SUBMITTING").execute().data
    for application in submitting:
        if _worker_heartbeat_is_stale(client, application.get("worker_id"), max_heartbeat_age_seconds):
            client.table("applications").update(
                {
                    "status": "FAILED_RETRYABLE",
                    "error_code": "SUBMISSION_UNCERTAIN_AFTER_CRASH",
                    "error_message": (
                        "Worker crashed while submitting -- whether Submit was actually clicked is unknown. "
                        "Verify on Dice's Applied Jobs page before requeuing."
                    ),
                }
            ).eq("id", application["id"]).execute()
            recovered["needs_verification"].append(application["id"])

    return recovered


def recover_orphaned_runs(max_heartbeat_age_seconds: int = DEFAULT_STALE_LEASE_SECONDS) -> list[str]:
    """Startup reconciliation for a run stuck RUNNING whose claiming
    worker's heartbeat has gone stale -- handed back to PENDING so a live
    daemon can reclaim it. Safe regardless of what its individual
    applications were doing: recover_stale_applications() (called first,
    same startup pass) already reconciles any PROCESSING/SUBMITTING
    application left behind, and claim_next_queued_application_for_run
    only ever claims QUEUED applications going forward."""
    client = get_supabase_client()
    running = client.table("application_runs").select("id, claimed_by").eq("status", "RUNNING").execute().data
    recovered = []
    for run in running:
        if _worker_heartbeat_is_stale(client, run.get("claimed_by"), max_heartbeat_age_seconds):
            client.table("application_runs").update({"status": "PENDING", "claimed_by": None, "claimed_at": None}).eq(
                "id", run["id"]
            ).execute()
            recovered.append(run["id"])
    return recovered


def reconcile_run_after_disconnect(run_id: str) -> dict[str, list[str]]:
    """Called immediately by the SAME worker that just had its own
    Playwright/CDP connection fail mid-run (Phase 7.3 -- Steel session
    recovery). Unlike recover_stale_applications()/recover_orphaned_runs()
    (which gate on heartbeat staleness, for a genuinely different worker
    process to safely determine a previous one died), this needs no such
    gate: the caller already knows with certainty its own connection
    just broke, so it reconciles right away rather than waiting for a
    future startup pass.

    Same classification as recover_stale_applications() and for the same
    reason: PROCESSING is entirely pre-Submit, safe to hand straight back
    to QUEUED. SUBMITTING crossed into the actual Submit attempt --
    whether the click landed is genuinely unknown, so this NEVER
    re-queues it (that would risk a second real Submit click); it lands
    on FAILED_RETRYABLE with an explicit reason, inert until a human
    verifies on Dice's own Applied Jobs page and deliberately requeues it
    themselves."""
    client = get_supabase_client()
    reconciled: dict[str, list[str]] = {"requeued": [], "needs_verification": []}

    for application in client.table("applications").select("id").eq("run_id", run_id).eq("status", "PROCESSING").execute().data:
        client.table("applications").update({"status": "QUEUED", "worker_id": None, "lock_acquired_at": None}).eq(
            "id", application["id"]
        ).execute()
        reconciled["requeued"].append(application["id"])

    for application in client.table("applications").select("id").eq("run_id", run_id).eq("status", "SUBMITTING").execute().data:
        client.table("applications").update(
            {
                "status": "FAILED_RETRYABLE",
                "error_code": "SUBMISSION_UNCERTAIN_AFTER_CRASH",
                "error_message": (
                    "Worker lost its browser connection while submitting -- whether Submit was actually clicked "
                    "is unknown. Verify on Dice's Applied Jobs page before requeuing."
                ),
            }
        ).eq("id", application["id"]).execute()
        reconciled["needs_verification"].append(application["id"])

    client.table("application_runs").update({"status": "PENDING", "claimed_by": None, "claimed_at": None}).eq("id", run_id).execute()
    return reconciled


def _application_ids_for_run(client, run_id: str) -> list[str]:
    rows = client.table("applications").select("id").eq("run_id", run_id).order("queued_at").execute().data
    return [r["id"] for r in rows]


def _to_run_dict(run_row: dict[str, Any], application_ids: list[str]) -> dict[str, Any]:
    return {
        "id": run_row["id"],
        "candidate_id": run_row["candidate_id"],
        "status": run_row["status"],
        "stop_requested": run_row.get("stop_requested", False),
        "claimed_by": run_row.get("claimed_by"),
        "claimed_at": run_row.get("claimed_at"),
        "submission_policy": run_row.get("submission_policy", "REQUIRE_CONFIRMATION"),
        "application_ids": application_ids,
        "created_at": run_row["created_at"],
        "updated_at": run_row["updated_at"],
    }
