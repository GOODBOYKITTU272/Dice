"""Phase 6.2: bounded-run identity for the Jobs selection -> worker flow.

No Supabase schema migration is available in this environment as of this
module's creation (2026-08-22) -- the linked `supabase` CLI session is
authenticated to a different account than the one that owns the DicePilot
project, so DDL (a new applications.run_id column + claim RPC) can't be
applied here. This is the "equivalent mechanism" instead: a local JSON
file per run, holding the exact application_ids that belong to it.

This is what guarantees the critical requirement -- "select 5 jobs" can
never silently become "process every queued job in Supabase" -- because
the worker never runs a DB pool query for this path at all; it only ever
iterates the specific ids stored here. Appropriate for this project's
existing architecture (single operator, single local worker process, no
distributed/multi-machine concern) rather than a shortcoming worth
over-engineering around.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RUNS_DIR = Path(__file__).resolve().parent / ".runtime" / "runs"


class RunNotFoundError(RuntimeError):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path(run_id: str) -> Path:
    return RUNS_DIR / f"{run_id}.json"


def create_run(application_ids: list[str], candidate_id: str) -> dict[str, Any]:
    """One run per "Apply to Selected Jobs" click. application_ids order
    is preserved and is the exact, and only, processing order/scope."""
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    run = {
        "id": str(uuid.uuid4()),
        "candidate_id": candidate_id,
        "application_ids": list(application_ids),
        "status": "QUEUED",  # QUEUED -> RUNNING -> COMPLETE | STOPPED
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    _path(run["id"]).write_text(json.dumps(run, indent=2))
    return run


def get_run(run_id: str) -> dict[str, Any]:
    path = _path(run_id)
    if not path.exists():
        raise RunNotFoundError(run_id)
    return json.loads(path.read_text())


def update_run_status(run_id: str, status: str) -> dict[str, Any]:
    run = get_run(run_id)
    run["status"] = status
    run["updated_at"] = _now_iso()
    _path(run_id).write_text(json.dumps(run, indent=2))
    return run


def is_stopped(run_id: str) -> bool:
    """Never raises -- a run that can't be found isn't a "stopped" run,
    it's an absent one; callers checking this mid-loop shouldn't crash
    on a lookup miss."""
    try:
        return get_run(run_id)["status"] == "STOPPED"
    except RunNotFoundError:
        return False
