"""Jobs 3-step selection flow (Discover -> Filter & Select -> Review &
Apply). Real Supabase, disposable TEST- rows cleaned up per test, same
convention as tests/test_local_app.py.

Phase 6.3: /jobs/apply only ever writes a PENDING run to Supabase and
redirects -- it never launches a worker process of any kind (see
tests/test_worker_daemon_architecture.py and
tests/test_jobs_apply_to_worker.py for that behavior's own coverage).
subprocess.Popen is still defensively monkeypatched to a no-op for every
test in this file (belt-and-suspenders against a regression reintroducing
a launch call), even though nothing here is expected to call it anymore.
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest

import local_app.app as app_module
import run_registry
from db.application_repository import get_supabase_client, update_application_status, upsert_dice_job
from local_app.app import app

APP_SOURCE = (Path(__file__).parent.parent / "local_app" / "app.py").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_real_worker_subprocess(monkeypatch):
    monkeypatch.setattr(app_module.subprocess, "Popen", lambda *a, **k: None)
    monkeypatch.setenv("DICEPILOT_CANDIDATE_ID", "44444444-4444-4444-4444-444444444444")


def _client():
    return app.test_client()


def _make_job(title, c2c="LIKELY", easy_apply=True):
    dice_job_id = f"TEST-{uuid.uuid4()}"
    return upsert_dice_job(
        {
            "dice_job_id": dice_job_id,
            "canonical_url": f"https://dice.com/job/{dice_job_id}",
            "title": title,
            "c2c_status": c2c,
            "is_easy_apply": easy_apply,
        }
    )


def _cleanup(*job_ids: str):
    # Multiple jobs from one Apply click share one run -- every
    # application across every given job_id must be deleted before any
    # run_id is deleted, or a still-referenced sibling application trips
    # the run_id FK.
    sc = get_supabase_client()
    all_run_ids: set[str] = set()
    for job_id in job_ids:
        apps = sc.table("applications").select("id, run_id").eq("dice_job_id", job_id).execute().data
        all_run_ids.update(a["run_id"] for a in apps if a.get("run_id"))
        for a in apps:
            sc.table("interventions").delete().eq("application_id", a["id"]).execute()
            sc.table("application_events").delete().eq("application_id", a["id"]).execute()
            sc.table("applications").delete().eq("id", a["id"]).execute()
        sc.table("dice_jobs").delete().eq("id", job_id).execute()
    for run_id in all_run_ids:
        sc.table("application_runs").delete().eq("id", run_id).execute()


# ── 1. Jobs route loads ────────────────────────────────────────────────


def test_jobs_route_loads():
    resp = _client().get("/jobs")
    assert resp.status_code == 200
    assert "Filter &amp; Select" in resp.get_data(as_text=True) or "Filter & Select" in resp.get_data(as_text=True)


# ── 2. role/count discovery form still works ────────────────────────────


def test_discovery_role_and_count_inputs_carry_through():
    body = _client().get("/jobs?last_role=Data+Engineer&last_max_results=7").get_data(as_text=True)
    assert 'value="Data Engineer"' in body
    assert 'value="7"' in body


# ── 3. filters still work ────────────────────────────────────────────────


def test_c2c_filter_excludes_non_matching_jobs():
    job = _make_job("TEST Filter Confirmed", c2c="CONFIRMED")
    try:
        body = _client().get("/jobs?c2c=NOT_C2C").get_data(as_text=True)
        assert "TEST Filter Confirmed" not in body
        body2 = _client().get("/jobs?c2c=CONFIRMED").get_data(as_text=True)
        assert "TEST Filter Confirmed" in body2
    finally:
        _cleanup(job["id"])


# ── 4/5. individual + Select All Filtered selection ─────────────────────


def test_eligible_job_has_enabled_checkbox_and_select_all_targets_only_enabled():
    job = _make_job("TEST Eligible Row")
    try:
        body = _client().get("/jobs").get_data(as_text=True)
        pattern = rf'<input[^>]*value="{job["id"]}"[^>]*>'
        match = re.search(pattern, body)
        assert match is not None
        assert "disabled" not in match.group(0)
        # selectAllFiltered() (see jobs.html) targets .job-checkbox:not(:disabled) --
        # confirms the class + selector this depends on are actually present.
        assert "job-checkbox" in body
        assert "selectAllFiltered" in body
    finally:
        _cleanup(job["id"])


# ── 6. Clear Selection works ─────────────────────────────────────────────


def test_clear_selection_control_and_handler_present():
    body = _client().get("/jobs").get_data(as_text=True)
    assert "clearSelection()" in body
    assert "function clearSelection" in body


# ── 7. submitted job cannot be selected ──────────────────────────────────


def test_submitted_job_checkbox_is_disabled():
    job = _make_job("TEST Submitted Row")
    from db.application_repository import enqueue_application

    candidate_id = str(uuid.uuid4())
    application = enqueue_application(candidate_id, job["id"])
    update_application_status(application["id"], "PROCESSING")
    update_application_status(application["id"], "SUBMITTING")
    update_application_status(application["id"], "SUBMITTED", submitted_at="2026-08-21T00:00:00Z")
    try:
        body = _client().get("/jobs").get_data(as_text=True)
        match = re.search(rf'<input[^>]*value="{job["id"]}"[^>]*>', body)
        assert match is not None
        assert "disabled" in match.group(0)
        assert "SUBMITTED" in body
    finally:
        _cleanup(job["id"])


# ── 8. skipped job cannot be selected ────────────────────────────────────


def test_skipped_job_checkbox_is_disabled():
    job = _make_job("TEST Skipped Row", easy_apply=False)
    try:
        body = _client().get("/jobs").get_data(as_text=True)
        match = re.search(rf'<input[^>]*value="{job["id"]}"[^>]*>', body)
        assert match is not None
        assert "disabled" in match.group(0)
        assert "SKIPPED" in body
    finally:
        _cleanup(job["id"])


# ── 9/10. selected count + breakdown counts accurate ─────────────────────


def test_review_counts_are_accurate():
    job_confirmed = _make_job("TEST Review Confirmed", c2c="CONFIRMED")
    job_likely = _make_job("TEST Review Likely", c2c="LIKELY")
    try:
        resp = _client().post("/jobs/review", data={"job_id": [job_confirmed["id"], job_likely["id"]]})
        body = resp.get_data(as_text=True)
        assert resp.status_code == 200
        # Two selected, one Confirmed, two Likely-or-Confirmed both Easy Apply.
        assert ">2<" in body  # Selected
        assert ">1<" in body  # Confirmed C2C
    finally:
        _cleanup(job_confirmed["id"], job_likely["id"])


# ── 11/12. Continue to Apply preserves IDs / review renders selection ────


def test_review_page_renders_exactly_the_selected_jobs_not_others():
    job_a = _make_job("TEST Selected Job A")
    job_b = _make_job("TEST Not Selected Job B")
    try:
        resp = _client().post("/jobs/review", data={"job_id": [job_a["id"]]})
        body = resp.get_data(as_text=True)
        assert "TEST Selected Job A" in body
        assert "TEST Not Selected Job B" not in body
    finally:
        _cleanup(job_a["id"], job_b["id"])


def test_review_with_no_selection_shows_empty_state():
    body = _client().post("/jobs/review", data={}).get_data(as_text=True)
    assert "Nothing selected" in body


# ── 13. remove selected job works ────────────────────────────────────────


def test_remove_form_resubmits_only_the_other_selected_ids():
    job_a = _make_job("TEST Remove Job A")
    job_b = _make_job("TEST Remove Job B")
    try:
        body = _client().post("/jobs/review", data={"job_id": [job_a["id"], job_b["id"]]}).get_data(as_text=True)
        # Job A's own Remove form must carry job_b's id (to keep it) but not job_a's own id (to drop it).
        row_a_start = body.index("TEST Remove Job A")
        row_a_end = body.index("</tr>", row_a_start)
        row_a_html = body[row_a_start:row_a_end]
        assert job_b["id"] in row_a_html
        assert job_a["id"] not in row_a_html
    finally:
        _cleanup(job_a["id"], job_b["id"])


# ── 14. Apply to Selected Jobs queues only the selected jobs ─────────────


def test_apply_queues_only_selected_jobs():
    job_selected = _make_job("TEST Apply Selected")
    job_not_selected = _make_job("TEST Apply Not Selected")
    try:
        resp = _client().post("/jobs/apply", data={"job_id": [job_selected["id"]]}, follow_redirects=False)
        assert resp.status_code == 302
        # Phase 6.2: a successful Apply now redirects to the Run Progress
        # page (/runs/<id>) instead of /applications?queued=N -- the queue
        # write itself is unchanged and still verified below.
        assert "/runs/" in resp.headers["Location"]

        sc = get_supabase_client()
        selected_apps = sc.table("applications").select("*").eq("dice_job_id", job_selected["id"]).execute().data
        not_selected_apps = (
            sc.table("applications").select("*").eq("dice_job_id", job_not_selected["id"]).execute().data
        )
        assert len(selected_apps) == 1
        assert selected_apps[0]["status"] == "QUEUED"
        assert len(not_selected_apps) == 0
    finally:
        _cleanup(job_selected["id"], job_not_selected["id"])


# ── 15. no duplicate application rows are created ────────────────────────


def test_apply_twice_does_not_create_duplicate_application_rows():
    job = _make_job("TEST Duplicate Apply")
    try:
        _client().post("/jobs/apply", data={"job_id": [job["id"]]}, follow_redirects=False)
        resp2 = _client().post("/jobs/apply", data={"job_id": [job["id"]]}, follow_redirects=False)
        assert resp2.status_code == 302
        assert "no_eligible_jobs=1" in resp2.headers["Location"]  # already had one -- DuplicateApplicationError caught, not queued again

        sc = get_supabase_client()
        apps = sc.table("applications").select("*").eq("dice_job_id", job["id"]).execute().data
        assert len(apps) == 1
    finally:
        _cleanup(job["id"])


def test_apply_skips_ineligible_job_even_if_submitted_in_form():
    """A stale client-side selection (job became SUBMITTED between page
    load and Apply) must not be queued -- the server re-checks eligibility,
    it doesn't trust the posted id blindly."""
    job = _make_job("TEST Stale Selection")
    from db.application_repository import enqueue_application

    candidate_id = str(uuid.uuid4())
    application = enqueue_application(candidate_id, job["id"])
    update_application_status(application["id"], "PROCESSING")
    update_application_status(application["id"], "SUBMITTING")
    update_application_status(application["id"], "SUBMITTED", submitted_at="2026-08-21T00:00:00Z")
    try:
        resp = _client().post("/jobs/apply", data={"job_id": [job["id"]]}, follow_redirects=False)
        assert "no_eligible_jobs=1" in resp.headers["Location"]
        sc = get_supabase_client()
        apps = sc.table("applications").select("*").eq("dice_job_id", job["id"]).execute().data
        assert len(apps) == 1  # still just the one SUBMITTED row, nothing new queued
    finally:
        _cleanup(job["id"])


# ── 16/17. no live Dice mutation / no parallel-worker behavior ───────────
# Structural boundary checks, matching this project's established pattern
# (tests/test_phase6_boundary.py) -- the Jobs selection routes must never
# import or call anything that drives a browser or the worker's execution
# path; only enqueue_application() (a DB write) is permitted.


def _route_body(function_name: str) -> str:
    match = re.search(rf"\ndef {function_name}\(.*?\):.*?(?=\n@app\.route|\Z)", APP_SOURCE, re.DOTALL)
    assert match is not None, f"route function {function_name!r} not found"
    return match.group(0)


def test_jobs_routes_never_import_browser_or_worker_execution_code():
    # Scoped to the Jobs selection flow's own route bodies -- NOT the whole
    # file, which legitimately references playwright/the worker CLI
    # elsewhere (browser_check.py's checks, "Resume Application"'s
    # subprocess launch). jobs(), jobs_review(), and (as of Phase 6.3)
    # jobs_apply() all stay fully DB-only.
    forbidden = ("playwright", "sync_playwright", "subprocess", "popen")
    for route_fn in ("jobs()", "jobs_review()", "jobs_apply()"):
        body = _route_body(route_fn.rstrip("()")).lower()
        for term in forbidden:
            assert term.lower() not in body, f"{route_fn} must never reference {term!r} -- Jobs selection must stay DB-only"

    for route_fn in ("jobs()", "jobs_review()", "jobs_apply()"):
        body = _route_body(route_fn.rstrip("()")).lower()
        for term in ("dice_browser.worker", "dice_browser.submission", "run_worker", "process_one_application"):
            assert term not in body, f"{route_fn} must never reference {term!r} -- Jobs selection must stay DB-only"


def test_jobs_apply_route_only_enqueues_and_creates_a_pending_run():
    # Phase 6.3: Apply to Selected Jobs never launches a worker process of
    # any kind -- it only enqueues applications and writes a PENDING run
    # (see tests/test_worker_daemon_architecture.py for the standalone
    # daemon that actually claims and processes it).
    body = _route_body("jobs_apply")
    assert "enqueue_application" in body
    assert "create_run" in body
    assert ".popen(" not in body.lower()
