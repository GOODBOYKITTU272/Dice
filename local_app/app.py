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

from flask import Flask, jsonify, render_template, request  # noqa: E402

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
    ("Phase 4E — Candidate Adapter", "NOT STARTED"),
    ("Phase 4F — NEEDS_INPUT", "NOT STARTED"),
    ("Phase 5 — Submission Verification", "NOT STARTED"),
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
    ("Auto-answering", "NOT BUILT"),
    ("Submission", "NOT BUILT"),
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
