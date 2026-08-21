"""DicePilot Phase 2 local operator UI.

Deliberately not a copy of the old Indeed app.py — new, small, and scoped
to exactly what Phase 2 needs: trigger discovery, show results. Runs
locally only; nothing here is meant to be deployed.

Discovery runs synchronously in the request (a handful of HTTP calls for
3-5 jobs takes a few seconds) — Phase 1's TRD warning about not running
long-lived browser workers inside Flask request threads doesn't apply
here, since there's no persistent browser and no long-lived session; this
is a short-lived HTTP fetch, not the Dice application worker.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import Flask, jsonify, redirect, render_template, request, url_for  # noqa: E402

from db.intervention_repository import (  # noqa: E402
    AlreadyResolvedError,
    InterventionNotFoundError,
    InvalidAnswerError,
    resolve_question_intervention,
)
from db.supabase_client import get_supabase_client  # noqa: E402
from dice.discovery import run_discovery  # noqa: E402

app = Flask(__name__)

DEFAULT_ROLE = "Software Engineer"
DEFAULT_MAX_RESULTS = 5

# Static status boards (Phase 4B) — recorded verification results, not a
# live worker poll. No worker/browser-control process exists yet to poll;
# see STATE.md for what each status reflects and when it was last verified.
DELIVERY_BOARD = [
    ("Phase 1 — Database/Foundation", "COMPLETE"),
    ("Phase 2 — Discovery/Qualification", "COMPLETE"),
    ("Phase 3A — Safe JobSpy Integration", "COMPLETE"),
    ("Phase 3B — Qualification Validation", "COMPLETE"),
    ("Phase 3C — C2C Correctness", "COMPLETE"),
    ("Phase 3D — LIKELY Policy", "COMPLETE"),
    ("Phase 4A — Reference Audit", "COMPLETE"),
    ("Phase 4B — Persistent Dice Browser", "COMPLETE"),
    ("Phase 4B.1 — Authenticated Session Bootstrap", "COMPLETE"),
    ("Phase 4C — Easy Apply + Resume", "COMPLETE"),
    ("Phase 4D — Question Engine", "EXTRACTION FOUNDATION COMPLETE — RADIO/TEXTAREA live-observed + offline-replay verified; select/date/checkbox/multi-select not yet observed"),
    ("Phase 4E — Candidate Adapter", "COMPLETE (offline) — live fetch BLOCKED, APPLYWIZZ_API_BASE_URL/TOKEN not configured"),
    ("Phase 4F — NEEDS_INPUT", "COMPLETE — live-verified NEEDS_INPUT -> resolved -> RESUMABLE, no schema migration"),
    ("Phase 5 — Submission Verification", "IMPLEMENTED, LIVE-EXERCISED — 1 live attempt correctly ended SUBMIT_FAILED (Dice's own error), not a false SUBMITTED"),
    ("Phase 6 — Sequential Worker", "NOT STARTED"),
    ("Phase 7 — 20-Job End-to-End", "NOT STARTED"),
]

BROWSER_STATUS = [
    ("Browser Foundation", "COMPLETE"),
    ("Persistent Profile", "VERIFIED"),
    ("Authentication", "VERIFIED — HUMAN + CDP"),  # CDP-attach to a normal, never-quit Chrome -- see STATE.md
    ("Browser Worker", "NOT RUNNING"),
    ("Easy Apply Entry", "VERIFIED"),
    ("Resume Detection", "VERIFIED"),
    ("Resume Replacement", "VERIFIED"),
    ("Resume Upload", "VERIFIED"),
    ("No-question branch (NO_QUESTIONS_PRESENT)", "LIVE VERIFIED"),
    ("Review-screen detection", "LIVE VERIFIED"),
    ("Radiogroup extraction", "LIVE OBSERVED + OFFLINE REPLAY VERIFIED"),
    ("Textarea extraction", "LIVE OBSERVED + OFFLINE REPLAY VERIFIED"),
    ("NEEDS_INPUT classification", "VERIFIED"),
    ("Select", "NOT LIVE VERIFIED"),
    ("Checkbox question", "NOT LIVE VERIFIED"),
    ("Date", "NOT LIVE VERIFIED"),
    ("Multi-select", "NOT LIVE VERIFIED"),
    ("Candidate source", "ApplyWizz existing candidate-details API"),
    ("Duplicate candidate truth store", "NO"),
    ("Pause on unknown", "VERIFIED"),
    ("Intervention dedupe", "VERIFIED"),
    ("Human resolution", "VERIFIED"),
    ("Resume eligibility", "VERIFIED"),
    ("External notification", "NOT BUILT"),
    ("Live Dice answer filling", "NOT BUILT"),
    ("Auto-answering", "NOT BUILT"),
    ("Submit control", "IMPLEMENTED"),
    ("Submission verification", "LIVE VERIFIED — 1 attempt, correctly classified SUBMIT_FAILED"),
    ("Live submission success (SUBMITTED)", "NOT YET LIVE VERIFIED"),
]


@app.route("/")
def index():
    return render_template(
        "index.html",
        default_role=DEFAULT_ROLE,
        default_max_results=DEFAULT_MAX_RESULTS,
        delivery_board=DELIVERY_BOARD,
        browser_status=BROWSER_STATUS,
    )


@app.route("/interventions")
def interventions_view():
    """Read-mostly Phase 4F operator view: open interventions (questions
    Phase 4D couldn't answer automatically), with a local-only resolve
    form. Never talks to Dice -- resolving here only records an answer
    in Supabase for a later phase to consume."""
    try:
        client = get_supabase_client()
        # Capped, not because V1 expects many open interventions, but
        # because the linked project has accumulated OPEN rows from past
        # test runs that were never cleaned up (a pre-existing gap, not
        # something this phase caused or fixes) -- without a cap this
        # view's N+1 job/application lookups make the page impractically
        # slow.
        open_interventions = (
            client.table("interventions")
            .select("*")
            .eq("status", "OPEN")
            .order("created_at", desc=True)
            .limit(20)
            .execute()
            .data
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the operator, not swallowed
        return render_template("interventions.html", interventions=[], error=str(exc))

    rows = []
    for iv in open_interventions:
        app_rows = client.table("applications").select("*").eq("id", iv["application_id"]).execute().data
        application = app_rows[0] if app_rows else {}
        job = {}
        if application.get("dice_job_id"):
            job_rows = (
                client.table("dice_jobs")
                .select("title, company_name")
                .eq("id", application["dice_job_id"])
                .execute()
                .data
            )
            job = job_rows[0] if job_rows else {}
        options = iv.get("options") or {}
        rows.append(
            {
                "intervention_id": iv["id"],
                "application_id": iv["application_id"],
                "job_title": job.get("title"),
                "company_name": job.get("company_name"),
                "question_prompt": iv.get("question_text"),
                "field_type": options.get("field_type"),
                "reason": options.get("reason"),
                "sensitivity": bool(options.get("sensitivity")),
                "choices": options.get("choices"),
                "status": iv["status"],
            }
        )
    return render_template("interventions.html", interventions=rows, error=None)


@app.route("/interventions/<intervention_id>/resolve", methods=["POST"])
def resolve_intervention_view(intervention_id):
    """Local/internal only -- records a human-supplied answer against one
    intervention. Never fills or clicks anything on Dice; a later phase
    consumes the resolved answer."""
    answer = (request.form.get("answer") or "").strip()
    try:
        resolve_question_intervention(intervention_id, answer, source="operator")
    except (InvalidAnswerError, AlreadyResolvedError, InterventionNotFoundError):
        pass  # surfaced implicitly: the intervention stays listed if unresolved
    return redirect(url_for("interventions_view"))


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


if __name__ == "__main__":
    app.run(debug=True, port=5050)
