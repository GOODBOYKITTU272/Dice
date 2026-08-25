"""Phase 7.1: Mac prototype -> persistent cloud worker migration. Reuses
the entire Phase 6 application engine and Supabase run architecture
unchanged -- only the runtime/deployment layer (CDP endpoint, browser
profile location, heartbeat states, crash recovery) is new. Covers the
20 explicit requirements from the "cloud worker migration" task:

 1. cloud worker config loads from env (DICEPILOT_CDP_URL)
 2. browser profile path is persistent/configurable (DICEPILOT_BROWSER_PROFILE_DIR)
 3. daemon no longer assumes user's Mac (same as 1 -- no hardcoded host)
 4. service starts daemon without CLI policy decision (existing coverage:
    tests/test_run_submission_policy.py::test_daemon_cli_flag_defaults_to_no_override)
 5. per-run policy still drives submission (existing coverage:
    tests/test_run_submission_policy.py)
 6. worker heartbeat visible to frontend (existing coverage:
    tests/test_run_progress_ux.py, extended here for the new states)
 7. browser disconnected visible to frontend
 8. auth required visible to frontend
 9. security challenge visible to frontend
10. worker restart preserves pending runs (PENDING rows are plain Supabase
    state, untouched by any daemon process existing or not -- structural,
    not re-tested here)
11. stale run lease recovery
12. stale pre-submit application can recover safely
13. possible-post-submit stale application is NOT blindly retried
14. exact selected-run scoping preserved (existing coverage, unaffected
    by this change: tests/test_worker_daemon_architecture.py)
15. no parallel browser mutation (existing coverage: run_worker_for_run's
    sequential while-loop, unaffected)
16. one Submit maximum preserved (existing coverage: dice_browser/
    submission.py's own no-retry design, unaffected)
17. VERIFICATION_UNCERTAIN no auto-retry (existing coverage:
    tests/test_worker.py, unaffected)
18. Resume Run still works (existing coverage: tests/test_run_registry.py,
    tests/test_jobs_apply_to_worker.py, unaffected)
19. Stop Run still works (existing coverage, unaffected)
20. no real Dice mutation in tests (mocked/offline throughout this file)
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

import dice_browser.worker_daemon as worker_daemon
import run_registry
from db.application_repository import (
    enqueue_application,
    get_application,
    get_supabase_client,
    update_application_status,
    upsert_dice_job,
)
from local_app.app import app

CANDIDATE = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def _client():
    return app.test_client()


def _make_job_and_application(title):
    dice_job_id = f"TEST-{uuid.uuid4()}"
    job = upsert_dice_job(
        {"dice_job_id": dice_job_id, "canonical_url": f"https://dice.com/job/{dice_job_id}", "title": title, "is_easy_apply": True}
    )
    application = enqueue_application(CANDIDATE, job["id"])
    return job, application


def _cleanup(*job_ids: str):
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


def _cleanup_heartbeats(*worker_ids: str):
    sc = get_supabase_client()
    for worker_id in worker_ids:
        sc.table("worker_heartbeats").delete().eq("worker_id", worker_id).execute()


def _write_stale_heartbeat(worker_id: str, minutes_ago: int = 10) -> None:
    sc = get_supabase_client()
    stale = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    sc.table("worker_heartbeats").insert({"worker_id": worker_id, "status": "ONLINE", "last_heartbeat_at": stale}).execute()


# 1 & 3. Cloud worker CDP endpoint loads from DICEPILOT_CDP_URL, not a
# bare hardcoded Mac-only literal.
def test_daemon_cdp_url_loads_from_env(monkeypatch):
    monkeypatch.setenv("DICEPILOT_CDP_URL", "http://10.0.1.5:9333")
    assert worker_daemon.default_cdp_url() == "http://10.0.1.5:9333"


def test_daemon_cdp_url_defaults_to_same_host_when_unset(monkeypatch):
    monkeypatch.delenv("DICEPILOT_CDP_URL", raising=False)
    assert worker_daemon.default_cdp_url() == "http://127.0.0.1:9333"


# 2. Browser profile path is configurable and durable (not /tmp).
def test_browser_profile_dir_loads_from_env(monkeypatch, tmp_path):
    from dice_browser.session import profile_dir_for

    monkeypatch.setenv("DICEPILOT_BROWSER_PROFILE_DIR", str(tmp_path))
    assert profile_dir_for("primary-candidate") == tmp_path / "primary-candidate"


def test_browser_profile_dir_defaults_to_repo_runtime_when_unset(monkeypatch):
    from dice_browser.session import DEFAULT_PROFILE_ROOT, profile_dir_for

    monkeypatch.delenv("DICEPILOT_BROWSER_PROFILE_DIR", raising=False)
    assert profile_dir_for("primary-candidate") == DEFAULT_PROFILE_ROOT / "primary-candidate"


# 7, 8, 9. Frontend-visible worker states beyond plain ONLINE/OFFLINE.
def test_worker_status_surfaces_auth_required_while_online():
    worker_id = f"TEST-worker-{uuid.uuid4()}"
    try:
        run_registry.write_heartbeat(worker_id, status="AUTH_REQUIRED")
        status = run_registry.worker_status()
        assert status["online"] is True  # the daemon process itself is alive
        assert status["status"] == "AUTH_REQUIRED"  # but truthfully reports the real problem
    finally:
        _cleanup_heartbeats(worker_id)


def test_worker_status_surfaces_security_challenge_while_online():
    worker_id = f"TEST-worker-{uuid.uuid4()}"
    try:
        run_registry.write_heartbeat(worker_id, status="SECURITY_CHALLENGE")
        status = run_registry.worker_status()
        assert status["online"] is True
        assert status["status"] == "SECURITY_CHALLENGE"
    finally:
        _cleanup_heartbeats(worker_id)


def test_worker_status_surfaces_browser_disconnected_while_online():
    worker_id = f"TEST-worker-{uuid.uuid4()}"
    try:
        run_registry.write_heartbeat(worker_id, status="BROWSER_DISCONNECTED")
        status = run_registry.worker_status(worker_id=worker_id)
        assert status["online"] is True
        assert status["status"] == "BROWSER_DISCONNECTED"
    finally:
        _cleanup_heartbeats(worker_id)


def test_run_progress_shows_dice_login_required():
    worker_id = f"TEST-worker-{uuid.uuid4()}"
    job, app_ = _make_job_and_application("TEST Cloud AuthRequired")
    run = run_registry.create_run([app_["id"]], candidate_id=CANDIDATE, submission_policy="AUTHORIZED_AUTONOMOUS")
    try:
        run_registry.write_heartbeat(worker_id, status="AUTH_REQUIRED")
        body = _client().get(f"/runs/{run['id']}").get_data(as_text=True)
        assert "Dice Login Required" in body
    finally:
        _cleanup(job["id"])
        _cleanup_heartbeats(worker_id)


def test_run_progress_shows_security_challenge(monkeypatch):
    """Monkeypatches run_registry.worker_status directly rather than
    relying on a real heartbeat write winning a "latest" race -- the
    real production worker (dice-worker) heartbeats continuously during
    a full local test run, so an unscoped worker_status() call (what
    local_app.queries.run_progress uses, correctly, for its single-
    worker-per-candidate production display) could otherwise pick up
    the real worker's ONLINE status instead of this test's own."""
    job, app_ = _make_job_and_application("TEST Cloud SecurityChallenge")
    run = run_registry.create_run([app_["id"]], candidate_id=CANDIDATE, submission_policy="AUTHORIZED_AUTONOMOUS")
    try:
        monkeypatch.setattr(run_registry, "worker_status", lambda *a, **kw: {"online": True, "status": "SECURITY_CHALLENGE"})
        body = _client().get(f"/runs/{run['id']}").get_data(as_text=True)
        assert "Security Challenge" in body
    finally:
        _cleanup(job["id"])


# 11. Stale run lease recovery -- a run stuck RUNNING with a dead worker
# is handed back to PENDING; a run whose worker is still alive is not.
def test_recover_orphaned_runs_recovers_dead_worker_run():
    dead_worker = f"TEST-worker-dead-{uuid.uuid4()}"
    job, app_ = _make_job_and_application("TEST Cloud OrphanedRun")
    run = run_registry.create_run([app_["id"]], candidate_id=CANDIDATE)
    try:
        client = get_supabase_client()
        client.table("application_runs").update({"status": "RUNNING", "claimed_by": dead_worker}).eq("id", run["id"]).execute()
        _write_stale_heartbeat(dead_worker, minutes_ago=10)

        recovered = run_registry.recover_orphaned_runs(max_heartbeat_age_seconds=90)
        assert run["id"] in recovered
        assert run_registry.get_run(run["id"])["status"] == "PENDING"
    finally:
        _cleanup(job["id"])
        _cleanup_heartbeats(dead_worker)


def test_recover_orphaned_runs_leaves_live_worker_run_alone():
    live_worker = f"TEST-worker-live-{uuid.uuid4()}"
    job, app_ = _make_job_and_application("TEST Cloud LiveRun")
    run = run_registry.create_run([app_["id"]], candidate_id=CANDIDATE)
    try:
        client = get_supabase_client()
        client.table("application_runs").update({"status": "RUNNING", "claimed_by": live_worker}).eq("id", run["id"]).execute()
        run_registry.write_heartbeat(live_worker, status="ONLINE")  # fresh -- still alive, just a long batch

        recovered = run_registry.recover_orphaned_runs(max_heartbeat_age_seconds=90)
        assert run["id"] not in recovered
        assert run_registry.get_run(run["id"])["status"] == "RUNNING"
    finally:
        _cleanup(job["id"])
        _cleanup_heartbeats(live_worker)


# 12. Stale pre-submit (PROCESSING) application recovers safely to QUEUED.
def test_recover_stale_applications_requeues_pre_submit_processing():
    dead_worker = f"TEST-worker-dead-{uuid.uuid4()}"
    job, app_ = _make_job_and_application("TEST Cloud StaleProcessing")
    try:
        update_application_status(app_["id"], "PROCESSING", worker_id=dead_worker)
        _write_stale_heartbeat(dead_worker, minutes_ago=10)

        recovered = run_registry.recover_stale_applications(max_heartbeat_age_seconds=90)
        assert app_["id"] in recovered["requeued"]
        refreshed = get_application(app_["id"])
        assert refreshed["status"] == "QUEUED"
        assert refreshed["worker_id"] is None
    finally:
        _cleanup(job["id"])
        _cleanup_heartbeats(dead_worker)


def test_recover_stale_applications_leaves_live_worker_processing_alone():
    live_worker = f"TEST-worker-live-{uuid.uuid4()}"
    job, app_ = _make_job_and_application("TEST Cloud LiveProcessing")
    try:
        update_application_status(app_["id"], "PROCESSING", worker_id=live_worker)
        run_registry.write_heartbeat(live_worker, status="ONLINE")

        recovered = run_registry.recover_stale_applications(max_heartbeat_age_seconds=90)
        assert app_["id"] not in recovered["requeued"]
        assert get_application(app_["id"])["status"] == "PROCESSING"
    finally:
        update_application_status(app_["id"], "FAILED", error_code="TEST_CLEANUP", error_message="test cleanup")
        _cleanup(job["id"])
        _cleanup_heartbeats(live_worker)


# 13. Possible-post-submit stale application (SUBMITTING) is NEVER
# blindly requeued -- lands on FAILED_RETRYABLE for human verification,
# never auto-retried by any daemon code path.
def test_recover_stale_applications_never_requeues_possible_post_submit():
    dead_worker = f"TEST-worker-dead-{uuid.uuid4()}"
    job, app_ = _make_job_and_application("TEST Cloud StaleSubmitting")
    try:
        update_application_status(app_["id"], "PROCESSING", worker_id=dead_worker)
        update_application_status(app_["id"], "SUBMITTING")
        _write_stale_heartbeat(dead_worker, minutes_ago=10)

        recovered = run_registry.recover_stale_applications(max_heartbeat_age_seconds=90)
        assert app_["id"] in recovered["needs_verification"]
        assert app_["id"] not in recovered["requeued"]
        refreshed = get_application(app_["id"])
        assert refreshed["status"] == "FAILED_RETRYABLE"
        assert refreshed["status"] != "QUEUED"
        assert refreshed["error_code"] == "SUBMISSION_UNCERTAIN_AFTER_CRASH"
    finally:
        _cleanup(job["id"])
        _cleanup_heartbeats(dead_worker)
