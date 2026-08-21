"""Read-only data-shaping for the local operator UI. Every function here
takes a Supabase client and returns plain dicts/lists ready for a
template -- no writes, no browser/worker action. Kept separate from
db/*.py because those modules are the write-path repositories used by the
worker; this module exists only to shape data for display, and to be
testable independent of Flask.
"""
from __future__ import annotations

from typing import Any

# applications.status values that represent "actively being worked" for
# the Worker/Dashboard "current job" widgets.
_RUNNING_STATUSES = ("PROCESSING", "SUBMITTING")
_FAILED_STATUSES = ("FAILED", "FAILED_RETRYABLE")

_TIMELINE_STEPS = (
    ("APPLICATION_CLAIMED", "Job claimed"),
    ("LIVE_QUALIFICATION_PASSED", "Live qualification"),
    ("AUTH_ACTIVE", "Authentication"),
    ("EASY_APPLY_OPENED", "Easy Apply opened"),
    ("RESUME_READY", "Resume checked"),
    ("QUESTIONS_CHECKED", "Questions checked"),
    ("REVIEW_READY", "Review reached"),
    ("SUBMIT_ATTEMPTED", "Submit clicked"),
    ("SUBMISSION_VERIFIED", "Submission verified"),
)

# The real event_type strings this codebase actually writes (worker.py /
# submission.py / questions.py) don't match the TRD-style names above
# 1:1 -- mapped here rather than renamed at the source, since the event
# log is an append-only audit trail nothing should rewrite.
_EVENT_TYPE_TO_STEP = {
    "easy_apply_opened": "EASY_APPLY_OPENED",
    "resume_uploaded": "RESUME_READY",
    "answer_filled_from_resolved_intervention": "QUESTIONS_CHECKED",
    "answer_auto_filled": "QUESTIONS_CHECKED",
    "awaiting_submit_confirmation": "REVIEW_READY",
    "submission_result": "SUBMISSION_VERIFIED",
}


def _job_by_id(client, dice_job_id: str | None) -> dict[str, Any]:
    if not dice_job_id:
        return {}
    rows = client.table("dice_jobs").select("*").eq("id", dice_job_id).execute().data
    return rows[0] if rows else {}


def _application_by_id(client, application_id: str | None) -> dict[str, Any]:
    if not application_id:
        return {}
    rows = client.table("applications").select("*").eq("id", application_id).execute().data
    return rows[0] if rows else {}


def _latest_event(client, application_id: str) -> dict[str, Any] | None:
    rows = (
        client.table("application_events")
        .select("*")
        .eq("application_id", application_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
        .data
    )
    return rows[0] if rows else None


# ── Dashboard ────────────────────────────────────────────────────────────


def dashboard_summary(client) -> dict[str, Any]:
    applications = client.table("applications").select("*").execute().data
    counts = application_counts(applications)

    running = [a for a in applications if a["status"] in _RUNNING_STATUSES]
    current_application = running[0] if running else None
    current_job = _job_by_id(client, current_application["dice_job_id"]) if current_application else None

    finished = sorted(
        (a for a in applications if a["status"] in ("SUBMITTED", "FAILED", "FAILED_RETRYABLE")),
        key=lambda a: a.get("updated_at") or "",
        reverse=True,
    )
    last_result = finished[0] if finished else None
    last_result_job = _job_by_id(client, last_result["dice_job_id"]) if last_result else None

    latest_events = (
        client.table("application_events").select("*").order("created_at", desc=True).limit(5).execute().data
    )
    for event in latest_events:
        event["_job"] = _job_by_id(client, _application_by_id(client, event["application_id"]).get("dice_job_id"))

    return {
        "counts": counts,
        "worker_status": "RUNNING" if current_application else "IDLE",
        "current_application": current_application,
        "current_job": current_job,
        "last_result": last_result,
        "last_result_job": last_result_job,
        "latest_events": latest_events,
    }


def application_counts(applications: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "submitted": sum(1 for a in applications if a["status"] == "SUBMITTED"),
        "needs_input": sum(1 for a in applications if a["status"] == "NEEDS_INPUT"),
        "failed": sum(1 for a in applications if a["status"] in _FAILED_STATUSES),
        "running": sum(1 for a in applications if a["status"] in _RUNNING_STATUSES),
        "queued": sum(1 for a in applications if a["status"] == "QUEUED"),
        "total": len(applications),
    }


# ── Jobs ─────────────────────────────────────────────────────────────────


def list_jobs(client) -> list[dict[str, Any]]:
    jobs = client.table("dice_jobs").select("*").order("discovered_at", desc=True).limit(100).execute().data
    applications = client.table("applications").select("id, dice_job_id, status").execute().data
    apps_by_job = {a["dice_job_id"]: a for a in applications}

    rows = []
    for job in jobs:
        app = apps_by_job.get(job["id"])
        rows.append({**job, "application": app, "current_state": _job_current_state(job, app)})
    return rows


def _job_current_state(job: dict[str, Any], application: dict[str, Any] | None) -> str:
    if job.get("c2c_status") == "NOT_C2C" or not job.get("is_easy_apply"):
        return "SKIPPED"
    if application is None:
        return "NEW"
    if application["status"] == "QUEUED":
        return "QUEUED"
    return "CLAIMED"


def job_detail(client, job_id: str) -> dict[str, Any] | None:
    job = _job_by_id(client, job_id)
    if not job:
        return None
    applications = client.table("applications").select("*").eq("dice_job_id", job_id).execute().data
    return {**job, "applications": applications}


# Jobs.current_state values a job may be selected for application from. Any
# other state (CLAIMED -- covers SUBMITTED/NEEDS_INPUT/PROCESSING/FAILED/
# already-applied, or SKIPPED -- not C2C or not Easy Apply) is not eligible.
SELECTABLE_STATES = ("NEW", "QUEUED")


def jobs_by_ids(client, job_ids: list[str]) -> list[dict[str, Any]]:
    """Same shape as list_jobs() rows, but scoped to an explicit id list and
    preserving that list's order -- used to render a job selection back
    (e.g. the Review & Apply screen) without re-deriving it from filters."""
    if not job_ids:
        return []
    jobs = client.table("dice_jobs").select("*").in_("id", job_ids).execute().data
    jobs_by_id = {j["id"]: j for j in jobs}
    applications = client.table("applications").select("id, dice_job_id, status").in_("dice_job_id", job_ids).execute().data
    apps_by_job = {a["dice_job_id"]: a for a in applications}

    rows = []
    for job_id in job_ids:
        job = jobs_by_id.get(job_id)
        if job is None:
            continue
        app = apps_by_job.get(job["id"])
        rows.append({**job, "application": app, "current_state": _job_current_state(job, app)})
    return rows


# ── Applications ─────────────────────────────────────────────────────────


def list_applications(client) -> dict[str, Any]:
    applications = (
        client.table("applications").select("*").order("updated_at", desc=True).limit(200).execute().data
    )
    rows = []
    for app in applications:
        job = _job_by_id(client, app["dice_job_id"])
        latest_event = _latest_event(client, app["id"])
        rows.append({**app, "job": job, "latest_event": latest_event})
    return {"counts": application_counts(applications), "rows": rows}


def application_detail(client, application_id: str) -> dict[str, Any] | None:
    application = _application_by_id(client, application_id)
    if not application:
        return None
    job = _job_by_id(client, application["dice_job_id"])
    events = (
        client.table("application_events")
        .select("*")
        .eq("application_id", application_id)
        .order("created_at")
        .execute()
        .data
    )
    interventions = (
        client.table("interventions").select("*").eq("application_id", application_id).execute().data
    )
    event_types = {e["event_type"] for e in events}
    completed_steps = {_EVENT_TYPE_TO_STEP[t] for t in event_types if t in _EVENT_TYPE_TO_STEP}
    if application["status"] in ("PROCESSING", "SUBMITTING", "SUBMITTED", "NEEDS_INPUT"):
        completed_steps.add("APPLICATION_CLAIMED")
        completed_steps.add("LIVE_QUALIFICATION_PASSED")
        completed_steps.add("AUTH_ACTIVE")
    # No dedicated event exists for "resume already on file" (only an
    # actual re-upload logs one) or for the raw Submit click itself (only
    # its classified result is logged) -- both are still knowable from
    # what DID get logged, so inferred here rather than left as a false
    # "not done" for a step that genuinely happened.
    if event_types & {"awaiting_submit_confirmation", "submission_result", "needs_input", "answer_filled_from_resolved_intervention", "answer_auto_filled"}:
        completed_steps.add("RESUME_READY")
        completed_steps.add("QUESTIONS_CHECKED")
    if "submission_result" in event_types:
        completed_steps.add("SUBMIT_ATTEMPTED")
    if application["status"] != "SUBMITTED":
        completed_steps.discard("SUBMISSION_VERIFIED")
    timeline = [{"step": step, "label": label, "done": step in completed_steps} for step, label in _TIMELINE_STEPS]

    return {
        "application": application,
        "job": job,
        "events": events,
        "interventions": interventions,
        "timeline": timeline,
    }


# ── Interventions ────────────────────────────────────────────────────────


def list_interventions(client, status: str = "OPEN") -> list[dict[str, Any]]:
    rows = (
        client.table("interventions")
        .select("*")
        .eq("status", status)
        .order("created_at", desc=True)
        .limit(50)
        .execute()
        .data
    )
    out = []
    for iv in rows:
        application = _application_by_id(client, iv["application_id"])
        job = _job_by_id(client, application.get("dice_job_id"))
        options = iv.get("options") or {}
        out.append(
            {
                **iv,
                "job": job,
                "field_type": options.get("field_type"),
                "reason": options.get("reason"),
                "sensitivity": bool(options.get("sensitivity")),
                "choices": options.get("choices"),
            }
        )
    return out


def intervention_detail(client, intervention_id: str) -> dict[str, Any] | None:
    rows = client.table("interventions").select("*").eq("id", intervention_id).execute().data
    if not rows:
        return None
    iv = rows[0]
    application = _application_by_id(client, iv["application_id"])
    job = _job_by_id(client, application.get("dice_job_id"))
    options = iv.get("options") or {}
    return {
        **iv,
        "application": application,
        "job": job,
        "field_type": options.get("field_type"),
        "reason": options.get("reason"),
        "sensitivity": bool(options.get("sensitivity")),
        "choices": options.get("choices"),
    }


# ── Events ───────────────────────────────────────────────────────────────


def list_events(client, application_id: str | None = None, event_type: str | None = None, failures_only: bool = False, submissions_only: bool = False) -> list[dict[str, Any]]:
    query = client.table("application_events").select("*").order("created_at", desc=True).limit(200)
    if application_id:
        query = query.eq("application_id", application_id)
    if event_type:
        query = query.eq("event_type", event_type)
    rows = query.execute().data

    if failures_only:
        rows = [e for e in rows if "fail" in e["event_type"].lower() or (e.get("metadata") or {}).get("status") in ("SUBMIT_FAILED", "AUTH_REQUIRED", "SECURITY_CHALLENGE")]
    if submissions_only:
        rows = [e for e in rows if e["event_type"] == "submission_result"]

    for event in rows:
        application = _application_by_id(client, event["application_id"])
        event["_job"] = _job_by_id(client, application.get("dice_job_id"))
    return rows


# ── Worker ───────────────────────────────────────────────────────────────


def worker_status_summary(client) -> dict[str, Any]:
    applications = client.table("applications").select("*").execute().data
    running = [a for a in applications if a["status"] in _RUNNING_STATUSES]
    current = running[0] if running else None
    current_job = _job_by_id(client, current["dice_job_id"]) if current else None
    return {
        "status": "RUNNING" if current else "IDLE",
        "current_application": current,
        "current_job": current_job,
        "counts": application_counts(applications),
    }


# ── Failure reasons (Applications page filter, not a separate page) ──────

FAILURE_REASON_TEXT = {
    "SUBMIT_FAILED": "Dice explicitly reported that the application could not be submitted.",
    "AUTH_REQUIRED": "Dice session requires authentication.",
    "STALE_INELIGIBLE": "Live listing no longer matches the stored C2C / Easy Apply qualification.",
    "VERIFICATION_UNCERTAIN": "Submit was attempted, but positive submission evidence could not be proven. Automatic retry disabled.",
    "SECURITY_CHALLENGE": "Dice security challenge detected. Human action required.",
    "RESUME_MISSING": "No resume on file and no resume path configured.",
    "RESUME_UPLOAD_FAILED": "Resume upload failed.",
    "EASY_APPLY_OPEN_FAILED": "Could not open the Easy Apply flow.",
    "DICE_JOB_NOT_FOUND": "The referenced Dice job could not be found.",
    "UNKNOWN_SCREEN": "The wizard screen was not recognized.",
    "REVIEW_NOT_REACHED": "The wizard did not reach a recognized Review screen.",
    "ANSWER_FILL_FAILED": "A resolved answer could not be filled into its control.",
    "ALREADY_APPLIED": "Dice reports this job as already applied.",
}


def failure_reason(application: dict[str, Any]) -> str:
    code = application.get("error_code")
    if not code:
        return application.get("error_message") or "No reason recorded."
    return FAILURE_REASON_TEXT.get(code, application.get("error_message") or code)


# ── Run Progress (Jobs selection -> worker) ───────────────────────────────


def _current_step_label(latest_event: dict[str, Any] | None) -> str:
    if latest_event is None:
        return "Live Qualification"
    step = _EVENT_TYPE_TO_STEP.get(latest_event["event_type"])
    for key, label in _TIMELINE_STEPS:
        if key == step:
            return label
    return latest_event["event_type"]


def run_progress(client, run: dict[str, Any]) -> dict[str, Any]:
    """Shapes one run_registry run for the Run Progress page. Reads
    real Supabase state for exactly the run's own application_ids --
    never a broader query -- in the run's original (selection) order."""
    application_ids: list[str] = run["application_ids"]
    applications = (
        client.table("applications").select("*").in_("id", application_ids).execute().data if application_ids else []
    )
    apps_by_id = {a["id"]: a for a in applications}

    rows = []
    for application_id in application_ids:
        application = apps_by_id.get(application_id)
        if application is None:
            continue
        job = _job_by_id(client, application.get("dice_job_id"))
        latest_event = _latest_event(client, application_id)
        open_intervention_id = None
        if application["status"] == "NEEDS_INPUT":
            open_rows = (
                client.table("interventions")
                .select("id")
                .eq("application_id", application_id)
                .eq("status", "OPEN")
                .limit(1)
                .execute()
                .data
            )
            open_intervention_id = open_rows[0]["id"] if open_rows else None
        rows.append(
            {
                **application,
                "job": job,
                "current_step_label": _current_step_label(latest_event),
                "open_intervention_id": open_intervention_id,
            }
        )

    running = [r for r in rows if r["status"] in _RUNNING_STATUSES]
    submitted = sum(1 for r in rows if r["status"] == "SUBMITTED")
    needs_input = sum(1 for r in rows if r["status"] == "NEEDS_INPUT")
    failed = sum(1 for r in rows if r["status"] in _FAILED_STATUSES)
    remaining = sum(1 for r in rows if r["status"] == "QUEUED")

    return {
        "run": run,
        "rows": rows,
        "current": running[0] if running else None,
        "counts": {
            "selected": len(rows),
            "processed": submitted + needs_input + failed,
            "submitted": submitted,
            "needs_input": needs_input,
            "failed": failed,
            "running": len(running),
            "remaining": remaining,
        },
    }
