"""DicePilot local operator UI.

Server-rendered Flask + Jinja, matching the existing project convention
(no build step, no second frontend truth store) -- every page reads
directly from the same Supabase project the worker writes to. Runs
locally only; nothing here is meant to be deployed.

Routes never drive Playwright directly (Phase 1's TRD warning about not
running long-lived browser workers inside Flask request threads) --
browser_check.py's checks are short-lived, read-only inspections of an
already-open CDP connection, and "Resume Application" launches the real
worker CLI as a detached subprocess rather than importing and running it
in-process.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import Flask, jsonify, redirect, render_template, request, url_for  # noqa: E402

import run_registry  # noqa: E402
from db.application_repository import DuplicateApplicationError, enqueue_application  # noqa: E402
from db.intervention_repository import (  # noqa: E402
    AlreadyResolvedError,
    InterventionNotFoundError,
    InvalidAnswerError,
    resolve_question_intervention,
)
from db.supabase_client import get_supabase_client  # noqa: E402
from dice.candidate_adapter import fetch_candidate  # noqa: E402
from dice.discovery import run_discovery  # noqa: E402
from local_app import browser_check, queries  # noqa: E402

app = Flask(__name__)
app.jinja_env.globals["failure_reason"] = queries.failure_reason

DEFAULT_ROLE = "Software Engineer"
DEFAULT_MAX_RESULTS = 5
CANDIDATE_ID_ENV_VAR = "DICEPILOT_CANDIDATE_ID"
_RESUME_PATH = str(Path(__file__).resolve().parent.parent / ".runtime" / "resume" / "test_resume.pdf")


def _authorized_candidate_id() -> str | None:
    """No hardcoded candidate id anywhere in this file -- configuration
    only. None means "not configured"; callers must show a clear error
    and refuse to enqueue/start a worker run, never fall back to a
    guessed or historical id."""
    return os.environ.get(CANDIDATE_ID_ENV_VAR)


def _client_or_none():
    try:
        return get_supabase_client(), None
    except Exception as exc:  # noqa: BLE001 - surfaced to the operator, not swallowed
        return None, str(exc)


# ── Dashboard ────────────────────────────────────────────────────────────


@app.route("/")
def dashboard():
    client, error = _client_or_none()
    summary = queries.dashboard_summary(client) if client else None
    return render_template("dashboard.html", active="dashboard", summary=summary, error=error)


# ── Jobs ─────────────────────────────────────────────────────────────────


@app.route("/jobs")
def jobs():
    client, error = _client_or_none()
    filters = {
        "c2c": request.args.get("c2c") or "",
        "easy_apply": request.args.get("easy_apply") or "",
        "state": request.args.get("state") or "",
        "company": request.args.get("company") or "",
    }
    rows = queries.list_jobs(client) if client else []
    if filters["c2c"]:
        rows = [r for r in rows if r["c2c_status"] == filters["c2c"]]
    if filters["easy_apply"] == "yes":
        rows = [r for r in rows if r["is_easy_apply"]]
    if filters["state"]:
        rows = [r for r in rows if r["current_state"] == filters["state"]]
    if filters["company"]:
        needle = filters["company"].lower()
        rows = [r for r in rows if needle in (r.get("company_name") or "").lower()]
    last_role = request.args.get("last_role") or DEFAULT_ROLE
    last_max_results = request.args.get("last_max_results") or str(DEFAULT_MAX_RESULTS)
    return render_template(
        "jobs.html", active="jobs", jobs=rows, filters=filters, error=error, last_role=last_role, last_max_results=last_max_results
    )


@app.route("/jobs/<uuid:job_id>")
def job_detail_view(job_id):
    # uuid converter (not a plain string) so this can never shadow the
    # literal, POST-only /jobs/apply and /jobs/review paths -- a GET to
    # either used to fall through to here with job_id="apply"/"review"
    # and 500 trying to query Supabase with an invalid UUID (found live
    # on the Vercel deployment, 2026-08-22).
    client, error = _client_or_none()
    job = queries.job_detail(client, str(job_id)) if client else None
    if job is None and not error:
        error = "Job not found."
    return render_template("job_detail.html", active="jobs", job=job, error=error)


@app.route("/jobs/review", methods=["POST"])
def jobs_review():
    """Screen 3 of the selection flow. Stateless -- the selected job_id set
    lives entirely in the submitted form (and is resubmitted whole by each
    "Remove" button), no server-side selection state anywhere. Read-only:
    creates no application rows, touches no Dice job."""
    client, error = _client_or_none()
    job_ids = [jid for jid in request.form.getlist("job_id") if jid]
    rows = queries.jobs_by_ids(client, job_ids) if client else []
    counts = {
        "total": len(rows),
        "confirmed": sum(1 for r in rows if r["c2c_status"] == "CONFIRMED"),
        "likely": sum(1 for r in rows if r["c2c_status"] == "LIKELY"),
        "easy_apply": sum(1 for r in rows if r["is_easy_apply"]),
    }
    return render_template("jobs_review.html", active="jobs", jobs=rows, counts=counts, error=error)


@app.route("/jobs/apply", methods=["POST"])
def jobs_apply():
    """Queues the selected, still-eligible jobs (enqueue_application() only
    -- the same call every other queued application in this project goes
    through) into a new bounded run_registry run, then launches the
    existing Phase 6 worker as a detached subprocess scoped to exactly
    that run. This route itself never touches Dice and never processes a
    job -- it only writes QUEUED rows and starts a worker process; the
    worker (run_worker_for_run) is what does the actual, still-sequential
    processing, one job at a time, using every existing safety gate
    unchanged."""
    candidate_id = _authorized_candidate_id()
    if not candidate_id:
        return render_template(
            "jobs_review.html",
            active="jobs",
            jobs=[],
            counts={"total": 0, "confirmed": 0, "likely": 0, "easy_apply": 0},
            error=f"{CANDIDATE_ID_ENV_VAR} is not configured -- cannot start a run. Set it in your environment/.env.",
        )

    client, error = _client_or_none()
    job_ids = [jid for jid in request.form.getlist("job_id") if jid]
    rows = queries.jobs_by_ids(client, job_ids) if client else []

    queued_application_ids: list[str] = []
    for row in rows:
        if row["current_state"] not in queries.SELECTABLE_STATES:
            continue  # re-checked server-side; a stale client selection can't queue an ineligible job
        try:
            application = enqueue_application(candidate_id, row["id"])
            queued_application_ids.append(application["id"])
        except DuplicateApplicationError:
            continue  # already has an application row -- not a duplicate, a no-op

    if not queued_application_ids:
        return redirect(url_for("applications", no_eligible_jobs=1))

    run = run_registry.create_run(queued_application_ids, candidate_id=candidate_id)

    try:
        subprocess.Popen(
            [sys.executable, "-m", "dice_browser.worker", "--run-id", run["id"], "--resume-path", _RESUME_PATH],
            cwd=str(Path(__file__).resolve().parent.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        # The run and its queued applications already exist in Supabase --
        # only the worker process itself failed to start (e.g. this Flask
        # process can't spawn a subprocess here, such as on a serverless
        # deployment). Land on Run Progress anyway with a specific error
        # rather than a generic 500 or a misleading "it's running" redirect.
        return redirect(url_for("run_progress_view", run_id=run["id"], launch_error=str(exc)))

    return redirect(url_for("run_progress_view", run_id=run["id"]))


@app.route("/runs/<run_id>")
def run_progress_view(run_id):
    try:
        run = run_registry.get_run(run_id)
    except run_registry.RunNotFoundError:
        return render_template("run_progress.html", active="jobs", run_id=run_id, progress=None, error="Run not found.")

    client, error = _client_or_none()
    progress = queries.run_progress(client, run) if client else None
    return render_template(
        "run_progress.html",
        active="jobs",
        run_id=run_id,
        progress=progress,
        error=error,
        launch_error=request.args.get("launch_error"),
    )


@app.route("/runs/<run_id>/stop", methods=["POST"])
def run_stop_view(run_id):
    """Sets the run's status to STOPPED -- checked by run_worker_for_run
    before it claims its NEXT application, never mid-flight. Cannot and
    does not interrupt a Submit/verification already in progress."""
    try:
        run_registry.update_run_status(run_id, "STOPPED")
    except run_registry.RunNotFoundError:
        pass
    return redirect(url_for("run_progress_view", run_id=run_id))


@app.route("/api/discover", methods=["POST"])
def api_discover():
    payload = request.get_json(silent=True) or {}
    role = (payload.get("role") or DEFAULT_ROLE).strip()
    try:
        max_results = int(payload.get("max_results") or DEFAULT_MAX_RESULTS)
    except (TypeError, ValueError):
        max_results = DEFAULT_MAX_RESULTS
    max_results = max(1, min(max_results, 20))  # Phase 1 V1 boundary: never more than 20

    rows = run_discovery(role=role, max_results=max_results)
    return jsonify({"role": role, "max_results": max_results, "jobs": rows})


# ── Applications ─────────────────────────────────────────────────────────


@app.route("/applications")
def applications():
    client, error = _client_or_none()
    data = queries.list_applications(client) if client else {"counts": queries.application_counts([]), "rows": []}
    status_filter = request.args.get("status") or ""
    rows = data["rows"]
    if status_filter:
        rows = [r for r in rows if r["status"] == status_filter]
    return render_template(
        "applications.html",
        active="applications",
        rows=rows,
        counts=data["counts"],
        status_filter=status_filter,
        error=error,
        no_eligible_jobs=request.args.get("no_eligible_jobs"),
    )


@app.route("/applications/<application_id>")
def application_detail_view(application_id):
    client, error = _client_or_none()
    detail = queries.application_detail(client, application_id) if client else None
    if detail is None and not error:
        error = "Application not found."
    if detail is None:
        return render_template(
            "application_detail.html", active="applications", error=error, application={}, job={}, events=[],
            interventions=[], open_interventions=[], timeline=[], display_status=None, current_step_label=None, run=None,
        )
    return render_template("application_detail.html", active="applications", error=None, **detail)


@app.route("/applications/<application_id>/resume", methods=["POST"])
def resume_application_view(application_id):
    """Resumes through the real worker CLI (dice_browser.worker
    --resume-application-id), launched as a detached subprocess -- never
    imports/runs the worker in-process inside this Flask request, and
    never just relinks to an old browser tab."""
    subprocess.Popen(
        [
            sys.executable,
            "-m",
            "dice_browser.worker",
            "--resume-application-id",
            application_id,
            "--resume-path",
            _RESUME_PATH,
        ],
        cwd=str(Path(__file__).resolve().parent.parent),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return redirect(url_for("application_detail_view", application_id=application_id))


# ── Interventions ────────────────────────────────────────────────────────


@app.route("/interventions")
def interventions_view():
    client, error = _client_or_none()
    rows = queries.list_interventions(client, status="OPEN") if client else []
    return render_template("interventions.html", active="interventions", interventions=rows, error=error)


@app.route("/interventions/<intervention_id>")
def intervention_detail_view(intervention_id):
    client, error = _client_or_none()
    iv = queries.intervention_detail(client, intervention_id) if client else None
    if iv is None and not error:
        error = "Intervention not found."
    return render_template("intervention_detail.html", active="interventions", iv=iv, error=error)


@app.route("/interventions/<intervention_id>/resolve", methods=["POST"])
def resolve_intervention_view(intervention_id):
    """Records a human-supplied answer against one intervention. Never
    fills or clicks anything on Dice; the worker's resume path consumes
    the resolved answer next time it runs."""
    answer = (request.form.get("answer") or "").strip()
    try:
        resolve_question_intervention(intervention_id, answer, source="operator")
    except (InvalidAnswerError, AlreadyResolvedError, InterventionNotFoundError):
        pass  # surfaced implicitly: the intervention stays listed if unresolved
    return redirect(url_for("interventions_view"))


# ── Events ───────────────────────────────────────────────────────────────

_EVENT_TYPES = [
    "easy_apply_opened", "resume_uploaded", "answer_filled_from_resolved_intervention",
    "answer_auto_filled", "awaiting_submit_confirmation", "submission_result",
]


@app.route("/events")
def events():
    client, error = _client_or_none()
    filters = {
        "event_type": request.args.get("event_type") or "",
        "failures_only": request.args.get("failures_only") == "1",
        "submissions_only": request.args.get("submissions_only") == "1",
    }
    rows = (
        queries.list_events(
            client,
            event_type=filters["event_type"] or None,
            failures_only=filters["failures_only"],
            submissions_only=filters["submissions_only"],
        )
        if client
        else []
    )
    return render_template("events.html", active="events", events=rows, filters=filters, event_types=_EVENT_TYPES, error=error)


# ── Worker ───────────────────────────────────────────────────────────────


@app.route("/worker")
def worker_view():
    client, error = _client_or_none()
    status = queries.worker_status_summary(client) if client else None
    return render_template("worker.html", active="worker", status=status, error=error)


# ── Candidate ────────────────────────────────────────────────────────────

_CANDIDATE_FIELDS = [
    ("Name", "name", False), ("Email", "email", False), ("Phone", "phone", False),
    ("Location", "location", False), ("Work Authorization", "work_authorized", True),
    ("Sponsorship Needed", "requires_sponsorship", True), ("Visa Type", "visa_type", True),
    ("Relocation", "willing_to_relocate", False), ("Experience", "experience_years", False),
    ("Earliest Start Date", "desired_start_date", False), ("LinkedIn", "linkedin_url", False),
    ("GitHub", "github_url", False), ("Resume", "resume_url", False),
]


@app.route("/candidate")
def candidate_view():
    configured = bool(os.environ.get("APPLYWIZZ_API_BASE_URL"))
    candidate_id = request.args.get("candidate_id") or _authorized_candidate_id()
    fetch_error = None
    fields = [(label, None, sensitive) for label, _, sensitive in _CANDIDATE_FIELDS]

    if configured and candidate_id:
        result = fetch_candidate(candidate_id)
        if result.profile is not None:
            fields = [(label, getattr(result.profile, attr), sensitive) for label, attr, sensitive in _CANDIDATE_FIELDS]
        else:
            fetch_error = result.error
    elif not candidate_id:
        fetch_error = f"{CANDIDATE_ID_ENV_VAR} is not configured -- set it in your environment/.env to view a candidate."

    return render_template(
        "candidate.html", active="candidate", configured=configured, fetch_error=fetch_error, candidate_id=candidate_id, fields=fields
    )


# ── Browser Session ──────────────────────────────────────────────────────


@app.route("/browser-session")
def browser_session_view():
    connection = browser_check.check_connection()
    return render_template(
        "browser_session.html",
        active="browser_session",
        cdp_url=browser_check.CDP_URL,
        connection=connection,
        full_check=browser_check.last_full_check(),
    )


@app.route("/browser-session/recheck", methods=["POST"])
def browser_session_recheck():
    browser_check.run_full_check()
    return redirect(url_for("browser_session_view"))


# ── Settings ─────────────────────────────────────────────────────────────


@app.route("/settings")
def settings_view():
    import os

    connection = browser_check.check_connection()
    return render_template(
        "settings.html",
        active="settings",
        candidate_source_configured=bool(os.environ.get("APPLYWIZZ_API_BASE_URL")),
        browser_connected=connection["connected"],
        resume_path=_RESUME_PATH,
    )


if __name__ == "__main__":
    app.run(debug=True, port=5050)
