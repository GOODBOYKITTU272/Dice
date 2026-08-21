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


@app.route("/")
def index():
    return render_template(
        "index.html", default_role=DEFAULT_ROLE, default_max_results=DEFAULT_MAX_RESULTS
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
