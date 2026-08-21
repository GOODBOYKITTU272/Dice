"""Jobs selection -> bounded worker run. Real Supabase for job/application
state (matching this project's established local_app test convention),
subprocess.Popen mocked so no real worker process (and therefore no real
Dice mutation) is ever launched by these tests.
"""
from __future__ import annotations

import uuid

import pytest

import local_app.app as app_module
import run_registry
from db.application_repository import get_supabase_client, update_application_status, upsert_dice_job
from local_app.app import app

TEST_CANDIDATE_ID = "22222222-2222-2222-2222-222222222222"


@pytest.fixture(autouse=True)
def _uses_real_run_registry(monkeypatch):
    # run_registry is Supabase-backed (migration 20260822010000_
    # application_runs.sql) -- nothing to isolate locally; kept as an
    # explicit fixture (rather than removed) so this file still fails
    # loudly and early if app_module.run_registry is ever monkeypatched
    # to something else by an unrelated change.
    monkeypatch.setattr(app_module, "run_registry", run_registry)


@pytest.fixture()
def configured_candidate(monkeypatch):
    monkeypatch.setenv("DICEPILOT_CANDIDATE_ID", TEST_CANDIDATE_ID)
    return TEST_CANDIDATE_ID


@pytest.fixture()
def fake_popen(monkeypatch):
    calls: list[list[str]] = []

    class _FakeProcess:
        pass

    def _fake_popen(cmd, **kwargs):
        calls.append(cmd)
        return _FakeProcess()

    monkeypatch.setattr(app_module.subprocess, "Popen", _fake_popen)
    return calls


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
    # Two jobs from one Apply click share one run -- every application
    # across every given job_id must be deleted before any run_id is
    # deleted, or a still-referenced sibling application (from a job_id
    # processed later in this same call) trips the run_id FK.
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


# 6. candidate ID comes from configuration
def test_candidate_id_comes_from_env_not_hardcoded():
    import inspect

    source = inspect.getsource(app_module)
    assert "23374e49" not in source  # the old hardcoded id must be gone entirely
    assert "DICEPILOT_CANDIDATE_ID" in source


# 7. missing candidate configuration blocks the run
def test_apply_without_candidate_config_blocks_run(monkeypatch, fake_popen):
    monkeypatch.delenv("DICEPILOT_CANDIDATE_ID", raising=False)
    job = _make_job("TEST No Candidate Config")
    try:
        resp = _client().post("/jobs/apply", data={"job_id": [job["id"]]})
        body = resp.get_data(as_text=True)
        assert "DICEPILOT_CANDIDATE_ID" in body
        assert "not configured" in body

        sc = get_supabase_client()
        apps = sc.table("applications").select("*").eq("dice_job_id", job["id"]).execute().data
        assert len(apps) == 0  # nothing queued
        assert fake_popen == []  # worker never launched
    finally:
        _cleanup(job["id"])


# 1/8. selected jobs queue correctly, run starts after Apply to Selected Jobs
def test_apply_queues_selected_jobs_and_starts_a_run(configured_candidate, fake_popen):
    job_a = _make_job("TEST Apply Run A")
    job_b = _make_job("TEST Apply Run B")
    try:
        resp = _client().post("/jobs/apply", data={"job_id": [job_a["id"], job_b["id"]]}, follow_redirects=False)
        assert resp.status_code == 302
        assert "/runs/" in resp.headers["Location"]

        run_id = resp.headers["Location"].rsplit("/", 1)[-1]
        run = run_registry.get_run(run_id)
        assert len(run["application_ids"]) == 2
        assert run["candidate_id"] == configured_candidate

        sc = get_supabase_client()
        apps = sc.table("applications").select("*").in_("dice_job_id", [job_a["id"], job_b["id"]]).execute().data
        assert len(apps) == 2
        assert all(a["status"] == "QUEUED" for a in apps)

        assert len(fake_popen) == 1
        assert "--run-id" in fake_popen[0]
        assert run_id in fake_popen[0]
    finally:
        _cleanup(job_a["id"], job_b["id"])


# 2. only selected IDs belong to the run
def test_run_contains_only_selected_application_ids(configured_candidate, fake_popen):
    job_selected = _make_job("TEST Run Scope Selected")
    job_not_selected = _make_job("TEST Run Scope Not Selected")
    try:
        resp = _client().post("/jobs/apply", data={"job_id": [job_selected["id"]]}, follow_redirects=False)
        run_id = resp.headers["Location"].rsplit("/", 1)[-1]
        run = run_registry.get_run(run_id)

        sc = get_supabase_client()
        selected_app = sc.table("applications").select("*").eq("dice_job_id", job_selected["id"]).execute().data[0]
        assert run["application_ids"] == [selected_app["id"]]

        not_selected_apps = sc.table("applications").select("*").eq("dice_job_id", job_not_selected["id"]).execute().data
        assert len(not_selected_apps) == 0  # never queued, never in the run
    finally:
        _cleanup(job_selected["id"], job_not_selected["id"])


# 3. unrelated pre-existing QUEUED application is not swept into the run
def test_preexisting_queued_application_not_swept_into_new_run(configured_candidate, fake_popen):
    from db.application_repository import enqueue_application

    job_preexisting = _make_job("TEST Preexisting Queued")
    job_selected = _make_job("TEST Newly Selected")
    preexisting_app = enqueue_application(configured_candidate, job_preexisting["id"])
    try:
        resp = _client().post("/jobs/apply", data={"job_id": [job_selected["id"]]}, follow_redirects=False)
        run_id = resp.headers["Location"].rsplit("/", 1)[-1]
        run = run_registry.get_run(run_id)
        assert preexisting_app["id"] not in run["application_ids"]
    finally:
        _cleanup(job_preexisting["id"], job_selected["id"])


# 4. duplicate application not created
def test_apply_does_not_create_duplicate_application_row(configured_candidate, fake_popen):
    job = _make_job("TEST No Duplicate Apply")
    try:
        _client().post("/jobs/apply", data={"job_id": [job["id"]]}, follow_redirects=False)
        _client().post("/jobs/apply", data={"job_id": [job["id"]]}, follow_redirects=False)
        sc = get_supabase_client()
        apps = sc.table("applications").select("*").eq("dice_job_id", job["id"]).execute().data
        assert len(apps) == 1
    finally:
        _cleanup(job["id"])


# 5. submitted/skipped job cannot enter a run
def test_submitted_job_cannot_enter_a_new_run(configured_candidate, fake_popen):
    from db.application_repository import enqueue_application

    job = _make_job("TEST Submitted Cannot Enter Run")
    application = enqueue_application(configured_candidate, job["id"])
    update_application_status(application["id"], "PROCESSING")
    update_application_status(application["id"], "SUBMITTING")
    update_application_status(application["id"], "SUBMITTED", submitted_at="2026-08-22T00:00:00Z")
    try:
        resp = _client().post("/jobs/apply", data={"job_id": [job["id"]]}, follow_redirects=False)
        assert resp.status_code == 302
        assert "no_eligible_jobs=1" in resp.headers["Location"]  # not queued into a run at all
        assert fake_popen == []  # no worker launched for zero queued jobs
    finally:
        _cleanup(job["id"])


def test_skipped_job_cannot_enter_a_new_run(configured_candidate, fake_popen):
    job = _make_job("TEST Skipped Cannot Enter Run", easy_apply=False)
    try:
        resp = _client().post("/jobs/apply", data={"job_id": [job["id"]]}, follow_redirects=False)
        assert "no_eligible_jobs=1" in resp.headers["Location"]
        assert fake_popen == []
    finally:
        _cleanup(job["id"])


# ── Run Progress page ──────────────────────────────────────────────────


def test_run_progress_page_shows_selected_and_current_job(configured_candidate, fake_popen):
    job = _make_job("TEST Run Progress Display")
    try:
        resp = _client().post("/jobs/apply", data={"job_id": [job["id"]]}, follow_redirects=False)
        run_id = resp.headers["Location"].rsplit("/", 1)[-1]

        body = _client().get(f"/runs/{run_id}").get_data(as_text=True)
        assert "TEST Run Progress Display" in body
        assert "QUEUED" in body
    finally:
        _cleanup(job["id"])


def test_run_progress_page_missing_run_shows_not_found():
    body = _client().get(f"/runs/{uuid.uuid4()}").get_data(as_text=True)
    assert "Run not found" in body


# 18. Stop Run prevents the next job from starting (worker-side coverage
# lives in tests/test_worker_run.py; this checks the route + registry wiring)
def test_stop_run_route_marks_run_stopped(configured_candidate, fake_popen):
    job = _make_job("TEST Stop Run Route")
    try:
        resp = _client().post("/jobs/apply", data={"job_id": [job["id"]]}, follow_redirects=False)
        run_id = resp.headers["Location"].rsplit("/", 1)[-1]

        stop_resp = _client().post(f"/runs/{run_id}/stop", follow_redirects=False)
        assert stop_resp.status_code == 302
        assert run_registry.get_run(run_id)["status"] == "STOPPED"
    finally:
        _cleanup(job["id"])


# ── 16/17/19/20/21/22: structural/boundary checks ───────────────────────
# Matches this project's established pattern (tests/test_phase6_boundary.py) --
# no browser/worker import in the Flask routes, only enqueue + subprocess launch.


def test_jobs_apply_route_never_imports_playwright_or_calls_worker_functions_directly():
    import re
    from pathlib import Path

    source = (Path(__file__).parent.parent / "local_app" / "app.py").read_text(encoding="utf-8")
    match = re.search(r"\ndef jobs_apply\(\):.*?(?=\n@app\.route|\Z)", source, re.DOTALL)
    assert match is not None
    body = match.group(0).lower()
    for forbidden in ("playwright", "sync_playwright", "run_worker_for_run(", "process_one_application("):
        assert forbidden not in body, f"jobs_apply must launch the worker as a subprocess, never call it in-process ({forbidden!r} found)"
    assert "subprocess.popen" in body
