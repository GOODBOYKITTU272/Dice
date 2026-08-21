"""Phase 6.2: bounded-run identity for the Jobs selection -> worker flow.

Backed by Supabase (application_runs table, applications.run_id column,
claim_next_queued_application_for_run() RPC -- migration
20260822010000_application_runs.sql) now that a Supabase CLI session
authorized for the DicePilot project is available. This module's public
interface is unchanged from its first version (a local JSON file, used
while that access wasn't available yet) -- nothing calling create_run/
get_run/update_run_status/is_stopped needed to change.

The critical guarantee is now enforced by the database itself, not just
application code: claim_next_queued_application_for_run() only ever
selects rows where applications.run_id = the given run, so "select 5
jobs" cannot become "process every queued job" even under concurrent
access, not merely by convention.
"""
from __future__ import annotations

from typing import Any

from db.supabase_client import get_supabase_client


class RunNotFoundError(RuntimeError):
    pass


def create_run(application_ids: list[str], candidate_id: str) -> dict[str, Any]:
    """One run per "Apply to Selected Jobs" click. Inserts the run row,
    then stamps run_id onto exactly the given (already-enqueued)
    application rows -- callers enqueue first, then create_run()."""
    client = get_supabase_client()
    run_row = client.table("application_runs").insert({"candidate_id": candidate_id, "status": "QUEUED"}).execute().data[0]
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


def update_run_status(run_id: str, status: str) -> dict[str, Any]:
    client = get_supabase_client()
    result = client.table("application_runs").update({"status": status}).eq("id", run_id).execute()
    if not result.data:
        raise RunNotFoundError(run_id)
    return get_run(run_id)


def is_stopped(run_id: str) -> bool:
    """Never raises -- a run that can't be found isn't a "stopped" run,
    it's an absent one; callers checking this mid-loop shouldn't crash
    on a lookup miss."""
    try:
        return get_run(run_id)["status"] == "STOPPED"
    except RunNotFoundError:
        return False


def _application_ids_for_run(client, run_id: str) -> list[str]:
    rows = client.table("applications").select("id").eq("run_id", run_id).order("queued_at").execute().data
    return [r["id"] for r in rows]


def _to_run_dict(run_row: dict[str, Any], application_ids: list[str]) -> dict[str, Any]:
    return {
        "id": run_row["id"],
        "candidate_id": run_row["candidate_id"],
        "status": run_row["status"],
        "application_ids": application_ids,
        "created_at": run_row["created_at"],
        "updated_at": run_row["updated_at"],
    }
