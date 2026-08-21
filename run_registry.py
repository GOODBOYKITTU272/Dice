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


def create_run(application_ids: list[str], candidate_id: str) -> dict[str, Any]:
    """One run per "Start Applications" click. Inserts the run row
    PENDING, then stamps run_id onto exactly the given (already-enqueued)
    application rows -- callers enqueue first, then create_run()."""
    client = get_supabase_client()
    run_row = client.table("application_runs").insert({"candidate_id": candidate_id, "status": "PENDING"}).execute().data[0]
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


def claim_next_pending_run(worker_id: str) -> dict[str, Any] | None:
    """Atomic: claims the oldest PENDING run (FOR UPDATE SKIP LOCKED,
    server-side), stamping claimed_by/claimed_at and moving it to
    RUNNING. Returns None when nothing is PENDING -- the daemon's normal
    "nothing to do yet" case, not an error."""
    client = get_supabase_client()
    rows = client.rpc("claim_next_pending_run", {"p_worker_id": worker_id}).execute().data
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


def get_latest_heartbeat() -> dict[str, Any] | None:
    client = get_supabase_client()
    rows = client.table("worker_heartbeats").select("*").order("last_heartbeat_at", desc=True).limit(1).execute().data
    return rows[0] if rows else None


def worker_status(max_age_seconds: int = 30) -> dict[str, Any]:
    """Used by the Run Progress page. Never assumes a worker is
    connected just because the web app itself is reachable -- OFFLINE
    unless a heartbeat row exists AND is fresher than max_age_seconds."""
    hb = get_latest_heartbeat()
    if hb is None:
        return {"online": False, "status": "OFFLINE", "last_heartbeat_at": None, "age_seconds": None}
    last_raw = hb["last_heartbeat_at"]
    last_dt = datetime.fromisoformat(last_raw.replace("Z", "+00:00")) if isinstance(last_raw, str) else last_raw
    age_seconds = (datetime.now(timezone.utc) - last_dt).total_seconds()
    online = age_seconds <= max_age_seconds and hb["status"] == "ONLINE"
    return {
        "online": online,
        "status": hb["status"] if online else "OFFLINE",
        "last_heartbeat_at": last_raw,
        "age_seconds": age_seconds,
    }


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
        "application_ids": application_ids,
        "created_at": run_row["created_at"],
        "updated_at": run_row["updated_at"],
    }
