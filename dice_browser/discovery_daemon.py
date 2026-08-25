"""Phase M9: always-on Dice discovery -- the one remaining manual step
after Phase M8's live single-user hardening. Periodically runs the
existing, UNMODIFIED dice.discovery.run_discovery() (never duplicated),
then routes each newly-discovered qualified job through the existing,
UNMODIFIED readiness.offer_job_if_ready() -- this module adds scheduling,
overlap prevention, and offer pacing only, never new discovery or
readiness logic.

Hosting decision: runs as a background thread inside the existing
dice-worker process (see worker_daemon.py::main()) rather than as a
separate Railway service -- the smallest robust option available given
this project's services are configured with a per-service Custom Start
Command set directly in Railway's dashboard, which isn't reachable from
this CLI session without extra tooling. Functionally identical to a
standalone always-on process: still Railway-hosted, still survives
independently of the Mac, still never requires a terminal command once
deployed.

Startup safety: a cycle only ever evaluates jobs run_discovery() just
returned -- it never sweeps the full, months-deep dice_jobs table. A
freshly-deployed daemon cannot dump a historical backlog, because it has
no code path that would even look at one.
"""
from __future__ import annotations

import os
import threading
import time

DEFAULT_INTERVAL_SECONDS = 900  # 15 minutes -- a sensible bounded single-user cadence, not continuous scraping
_MIN_INTERVAL_SECONDS = 60  # guards against a misconfigured env var causing rapid hammering
_INTERVAL_ENV_VAR = "DICE_DISCOVERY_INTERVAL_SECONDS"
_ROLE_ENV_VAR = "DICE_DISCOVERY_ROLE"
_LOCATION_ENV_VAR = "DICE_DISCOVERY_LOCATION"
DEFAULT_ROLE = "DevOps Engineer"
DEFAULT_LOCATION = "United States"
DEFAULT_MAX_RESULTS = 10

# Existing product behavior (Telegram UX), preserved here rather than
# reinvented: at most this many AWAITING_USER_DECISION cards outstanding
# at once. Discovery may find and persist more eligible jobs than this in
# one cycle -- the extra ones are simply left held (no application row),
# naturally reconsidered on a later cycle once capacity frees up.
MAX_UNRESOLVED_OFFERS = 2

_cycle_lock = threading.Lock()


def resolve_interval_seconds() -> int:
    raw = os.environ.get(_INTERVAL_ENV_VAR)
    if not raw:
        return DEFAULT_INTERVAL_SECONDS
    try:
        return max(_MIN_INTERVAL_SECONDS, int(raw))
    except ValueError:
        return DEFAULT_INTERVAL_SECONDS


def _unresolved_offer_count(candidate_id: str) -> int:
    from db.supabase_client import get_supabase_client

    client = get_supabase_client()
    rows = (
        client.table("applications")
        .select("id")
        .eq("candidate_id", candidate_id)
        .eq("status", "AWAITING_USER_DECISION")
        .execute()
        .data
    )
    return len(rows or [])


def _reconcile_primary_channel_delivery(candidate_id: str) -> int:
    """Permanent regression guard, real production finding 2026-08-25:
    an offer can end up with its only outbound JOB_OFFER event on a
    non-primary channel (e.g. sent during channel-testing before the
    primary was reconfigured) -- silently unreachable to a candidate who
    only checks their actual primary channel, occupying capacity
    forever with no way for them to ever act on it.

    Sweeps every still-AWAITING_USER_DECISION application for this
    candidate at the START of each cycle and redelivers to primary if
    missing. Cheap by construction (there are at most MAX_UNRESOLVED_
    OFFERS such rows at any time) and idempotent -- attention.service.
    ensure_offer_reached_primary_channel's own already_sent_outbound
    check means a normal cycle where everything's already correctly
    delivered redelivers nothing, every time, not just the first."""
    from attention.service import ensure_offer_reached_primary_channel
    from db.supabase_client import get_supabase_client

    client = get_supabase_client()
    rows = (
        client.table("applications")
        .select("id")
        .eq("candidate_id", candidate_id)
        .eq("status", "AWAITING_USER_DECISION")
        .execute()
        .data
        or []
    )
    return sum(1 for row in rows if ensure_offer_reached_primary_channel(row["id"]).get("redelivered"))


def _internal_job_id(external_dice_job_id: str) -> str | None:
    """run_discovery()'s own result rows key jobs by Dice's external job
    id (raw.dice_job_id) -- the readiness gate and applications.
    dice_job_id both key on dice_jobs.id, the internal row PK. Looked up
    fresh here rather than assumed, since the two are easy to confuse
    (a real mistake made and caught manually earlier this session)."""
    from db.supabase_client import get_supabase_client

    client = get_supabase_client()
    rows = client.table("dice_jobs").select("id").eq("dice_job_id", external_dice_job_id).execute().data
    return rows[0]["id"] if rows else None


def run_one_discovery_cycle(
    candidate_id: str,
    role: str = DEFAULT_ROLE,
    max_results: int = DEFAULT_MAX_RESULTS,
    location: str = DEFAULT_LOCATION,
    max_unresolved_offers: int = MAX_UNRESOLVED_OFFERS,
) -> dict:
    """One discovery pass: real search -> persist via the existing
    dice_jobs model -> offer each qualified, newly-returned job through
    the unmodified central readiness gate, up to the pacing cap. Never
    raises -- a transient discovery/provider failure is caught and
    reported in the summary so the daemon loop keeps running, not a
    reason to crash. Never advances an application past creating the
    AWAITING_USER_DECISION offer row itself (offer_job_if_ready's own,
    unchanged, responsibility) -- what happens after Apply is entirely
    the existing worker daemon's job, not this module's."""
    from dice.discovery import run_discovery
    from readiness import offer_job_if_ready
    from attention.routing import resolve_primary_provider

    started_at = time.monotonic()
    summary = {
        "inspected": 0,
        "qualified": 0,
        "offers_produced": 0,
        "held": 0,
        "skipped_capacity": 0,
        "no_channel": 0,
        "reconciled": 0,
        "error": None,
    }
    try:
        try:
            summary["reconciled"] = _reconcile_primary_channel_delivery(candidate_id)
        except Exception:  # noqa: BLE001 - reconciliation is best-effort; a failure here must never block real discovery
            pass

        rows = run_discovery(role=role, max_results=max_results, location=location, printer=lambda line: None)
        summary["inspected"] = len(rows)
        qualified_rows = [r for r in rows if r.get("is_qualified")]
        summary["qualified"] = len(qualified_rows)

        for row in qualified_rows:
            if _unresolved_offer_count(candidate_id) >= max_unresolved_offers:
                summary["skipped_capacity"] += 1
                continue

            internal_id = _internal_job_id(row["dice_job_id"])
            if internal_id is None:
                continue

            provider = resolve_primary_provider(candidate_id)
            if provider is None:
                summary["no_channel"] += 1
                continue

            result = offer_job_if_ready(provider, candidate_id, internal_id)
            if result.get("offered"):
                summary["offers_produced"] += 1
            else:
                summary["held"] += 1
    except Exception as exc:  # noqa: BLE001 - one bad cycle must never kill the daemon
        summary["error"] = f"{type(exc).__name__}: {exc}"

    summary["duration_seconds"] = round(time.monotonic() - started_at, 2)
    return summary


def run_daemon(
    candidate_id: str,
    interval_seconds: int | None = None,
    role: str | None = None,
    location: str | None = None,
    max_results: int = DEFAULT_MAX_RESULTS,
    max_iterations: int | None = None,
) -> None:
    """Heartbeat-free, deliberately -- discovery has no browser/auth
    state of its own to report; worker_daemon's existing heartbeat
    already reflects the real ONLINE/AUTH_REQUIRED/BROWSER_DISCONNECTED
    signal readiness itself depends on. max_iterations bounds the loop
    for tests only; production callers never pass it."""
    interval_seconds = interval_seconds if interval_seconds is not None else resolve_interval_seconds()
    role = role or os.environ.get(_ROLE_ENV_VAR, DEFAULT_ROLE)
    location = location or os.environ.get(_LOCATION_ENV_VAR, DEFAULT_LOCATION)
    print(f"Dice discovery daemon starting -- candidate_id={candidate_id}, role={role!r}, location={location!r}, interval={interval_seconds}s")

    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        iterations += 1
        if not _cycle_lock.acquire(blocking=False):
            print("Discovery: previous cycle still running -- skipping this tick")
            time.sleep(interval_seconds)
            continue
        try:
            summary = run_one_discovery_cycle(candidate_id, role=role, max_results=max_results, location=location)
            print(
                f"Discovery cycle done: inspected={summary['inspected']} qualified={summary['qualified']} "
                f"offers={summary['offers_produced']} held={summary['held']} "
                f"skipped_capacity={summary['skipped_capacity']} no_channel={summary['no_channel']} "
                f"reconciled={summary['reconciled']} "
                f"duration={summary['duration_seconds']}s error={summary['error']}"
            )
        finally:
            _cycle_lock.release()
        time.sleep(interval_seconds)


def start_background(candidate_id: str) -> threading.Thread:
    """The production entrypoint worker_daemon.py::main() calls -- runs
    the discovery loop as a daemon thread alongside the existing
    application-processing loop in the SAME Railway process, rather than
    a separate service (see module docstring for why)."""
    thread = threading.Thread(target=run_daemon, args=(candidate_id,), daemon=True, name="dice-discovery")
    thread.start()
    return thread
