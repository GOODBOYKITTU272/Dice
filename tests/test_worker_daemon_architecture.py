"""Phase 6.3: Vercel frontend / Supabase persistent runs / Mac worker
daemon architecture split. Covers the 18 explicit requirements from the
"stop trying to run the Dice worker inside Vercel" task:

 1. Vercel /jobs/apply never calls subprocess.Popen
 2. starting selected jobs creates a persistent run
 3. persistent run contains exact selected application IDs
 4. an unrelated QUEUED application is excluded
 5. worker daemon claims only one run
 6. worker daemon processes only that run's items
 7. sequential order preserved
 8. heartbeat written
 9. worker offline detected
10. browser disconnected safely reported (run handed back, not consumed)
11. NEEDS_INPUT does not block the next independent run item
12. STOP prevents the next item claim
13. SUBMITTED persisted
14. no duplicate submit
15. worker can be restarted/re-invoked safely (claim/poll loop, not just once)
16. no local-file dependency for production run state
17. Vercel frontend can reload and still see the same run
18. no real Dice mutation during any of this -- browser/navigation layer
    is mocked or simply never invoked throughout this file

Real Supabase (matching this project's established rule that atomic
claim/status behavior can't be meaningfully faked in-process), disposable
TEST- rows, cleaned up per test. dice_browser.worker_daemon's Playwright
connection and dice_browser.worker.run_worker_for_run are monkeypatched
everywhere in this file -- nothing here ever opens a real browser page or
navigates to Dice.
"""
from __future__ import annotations

import inspect
import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import dice_browser.worker_daemon as worker_daemon
import local_app.app as app_module
import run_registry
from db.application_repository import (
    InvalidStatusTransitionError,
    enqueue_application,
    get_application,
    get_supabase_client,
    update_application_status,
    upsert_dice_job,
)
from db.submission_repository import record_submission_result
from dice_browser.models import SubmissionResult, SubmissionStatus
from local_app.app import app

CANDIDATE = "99999999-9999-9999-9999-999999999999"


def _client():
    return app.test_client()


def _make_job_only(title):
    dice_job_id = f"TEST-{uuid.uuid4()}"
    return upsert_dice_job(
        {"dice_job_id": dice_job_id, "canonical_url": f"https://dice.com/job/{dice_job_id}", "title": title, "is_easy_apply": True}
    )


def _make_job_and_application(title):
    job = _make_job_only(title)
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


class _FakePlaywright:
    def stop(self):
        pass


def _fake_connect(cdp_url):
    return _FakePlaywright(), object()


def _cleanup_heartbeats(*worker_ids: str):
    sc = get_supabase_client()
    for worker_id in worker_ids:
        sc.table("worker_heartbeats").delete().eq("worker_id", worker_id).execute()


def _route_body(func) -> str:
    return inspect.getsource(func)


# 1. Vercel /jobs/apply never calls subprocess.Popen
def test_jobs_apply_never_calls_subprocess_popen():
    body = _route_body(app_module.jobs_apply)
    assert "Popen" not in body
    assert "subprocess" not in body


# 2 & 3. Starting selected jobs creates a persistent run with exactly the
# selected application IDs, and (17) the Vercel frontend can reload and
# still see the same run (Supabase, not a per-request/in-memory value).
def test_starting_selected_jobs_creates_persistent_run_with_exact_ids():
    job1 = _make_job_only("TEST Daemon Persist 1")
    job2 = _make_job_only("TEST Daemon Persist 2")
    import os

    os.environ["DICEPILOT_CANDIDATE_ID"] = CANDIDATE
    try:
        resp = _client().post("/jobs/apply", data={"job_id": [job1["id"], job2["id"]]}, follow_redirects=False)
        assert resp.status_code == 302
        run_id = resp.headers["Location"].rstrip("/").rsplit("/", 1)[-1]

        run = run_registry.get_run(run_id)
        assert run["status"] == "PENDING"
        applications = get_supabase_client().table("applications").select("id, dice_job_id").in_("id", run["application_ids"]).execute().data
        selected_job_ids = {a["dice_job_id"] for a in applications}
        assert selected_job_ids == {job1["id"], job2["id"]}

        # 17: reload the Vercel page for this run -- same data both times
        body1 = _client().get(f"/runs/{run_id}").get_data(as_text=True)
        body2 = _client().get(f"/runs/{run_id}").get_data(as_text=True)
        assert run_id in body1 and run_id in body2
        assert "PENDING" in body1 and "PENDING" in body2
    finally:
        _cleanup(job1["id"], job2["id"])


# 4. An unrelated QUEUED application (not part of the selection) is excluded
def test_unrelated_queued_application_excluded_from_run():
    job1, app1 = _make_job_and_application("TEST Daemon Selected")
    job2, app2 = _make_job_and_application("TEST Daemon Unrelated")
    try:
        run = run_registry.create_run([app1["id"]], candidate_id=CANDIDATE)
        assert app2["id"] not in run["application_ids"]
        unrelated = get_application(app2["id"])
        assert unrelated["run_id"] is None
    finally:
        _cleanup(job1["id"], job2["id"])


# 7. Sequential order preserved (selection/queued order, not insertion order of the set)
def test_run_preserves_selection_order():
    job1, app1 = _make_job_and_application("TEST Daemon Order A")
    job2, app2 = _make_job_and_application("TEST Daemon Order B")
    try:
        run = run_registry.create_run([app1["id"], app2["id"]], candidate_id=CANDIDATE)
        assert run["application_ids"] == [app1["id"], app2["id"]]
    finally:
        _cleanup(job1["id"], job2["id"])


# 5 & 6. Worker daemon claims and processes exactly one run per successful
# poll, then stops (doesn't loop-claim a second one in the same poll).
#
# claim_next_pending_run itself is monkeypatched here rather than exercised
# for real: the real RPC claims the globally oldest PENDING row across the
# WHOLE application_runs table, which in this shared live Supabase project
# also holds real, non-test run rows this test process must never claim or
# mutate (a real incident: an earlier version of this test called the real
# RPC and it claimed and altered two real user runs before being caught and
# repaired). The RPC's own atomic claim-scoping is proven by construction --
# it uses the identical FOR UPDATE SKIP LOCKED / SECURITY DEFINER pattern
# already covered, safely (scoped to a specific run_id), by claim_next_
# queued_application_for_run's tests in test_worker_run.py.
def test_daemon_claims_and_processes_exactly_one_run_per_poll(monkeypatch):
    worker_id = f"TEST-worker-{uuid.uuid4()}"
    fake_run = {"id": "TEST-fake-run-1", "candidate_id": CANDIDATE, "status": "RUNNING", "application_ids": [], "submission_policy": "REQUIRE_CONFIRMATION"}
    claim_calls = []

    def _fake_claim(wid):
        claim_calls.append(wid)
        return fake_run if len(claim_calls) == 1 else None

    seen_run_ids = []
    monkeypatch.setattr(run_registry, "claim_next_pending_run", _fake_claim)
    monkeypatch.setattr(worker_daemon, "run_worker_for_run", lambda page, run_id, *a, **kw: seen_run_ids.append(run_id))
    monkeypatch.setattr(worker_daemon, "_connect", _fake_connect)
    monkeypatch.setattr(worker_daemon, "_check_browser_and_auth", lambda cdp_url, provider: "ONLINE")
    try:
        worker_daemon.run_daemon(worker_id, max_iterations=2, poll_interval=0, auth_check_interval=0)
        assert claim_calls == [worker_id, worker_id]  # polled twice
        assert seen_run_ids == [fake_run["id"]]  # processed exactly once (second poll found nothing)
    finally:
        _cleanup_heartbeats(worker_id)


# 8. Heartbeat written (idle poll, nothing PENDING)
def test_daemon_writes_heartbeat_on_idle_poll(monkeypatch):
    worker_id = f"TEST-worker-{uuid.uuid4()}"
    monkeypatch.setattr(run_registry, "claim_next_pending_run", lambda wid: None)
    monkeypatch.setattr(worker_daemon, "_check_browser_and_auth", lambda cdp_url, provider: "ONLINE")
    try:
        worker_daemon.run_daemon(worker_id, max_iterations=1, poll_interval=0, auth_check_interval=0)
        hb = run_registry.get_latest_heartbeat()
        assert hb["worker_id"] == worker_id
        assert hb["status"] == "ONLINE"
    finally:
        _cleanup_heartbeats(worker_id)


# 9. Worker offline detected (stale/absent heartbeat)
def test_worker_offline_detected_when_heartbeat_stale():
    worker_id = f"TEST-worker-{uuid.uuid4()}"
    try:
        stale = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        get_supabase_client().table("worker_heartbeats").insert(
            {"worker_id": worker_id, "status": "ONLINE", "last_heartbeat_at": stale}
        ).execute()
        status = run_registry.worker_status(max_age_seconds=30)
        assert status["online"] is False
        assert status["status"] == "OFFLINE"
    finally:
        _cleanup_heartbeats(worker_id)


# 10. Browser disconnected safely reported -- run handed back to PENDING,
# never silently consumed, and (18) no real Playwright/Dice call is made.
# claim_next_pending_run and update_run_status are both monkeypatched --
# see the comment on test_daemon_claims_and_processes_exactly_one_run_per_poll
# for why this must never touch the shared live application_runs table.
def test_daemon_reports_browser_disconnected_and_hands_run_back_to_pending(monkeypatch):
    worker_id = f"TEST-worker-{uuid.uuid4()}"
    fake_run = {"id": "TEST-fake-run-2", "candidate_id": CANDIDATE, "status": "RUNNING", "application_ids": [], "submission_policy": "REQUIRE_CONFIRMATION"}
    status_updates = []

    def _boom(cdp_url):
        raise ConnectionError("no CDP endpoint reachable")

    monkeypatch.setattr(run_registry, "claim_next_pending_run", lambda wid: fake_run)
    monkeypatch.setattr(run_registry, "update_run_status", lambda run_id, status: status_updates.append((run_id, status)))
    monkeypatch.setattr(worker_daemon, "_connect", _boom)
    try:
        worker_daemon.run_daemon(worker_id, max_iterations=1, poll_interval=0, recovery_backoff_seconds=0)

        hb = run_registry.get_latest_heartbeat()
        assert hb["worker_id"] == worker_id
        assert hb["status"] == "BROWSER_DISCONNECTED"
        assert status_updates == [(fake_run["id"], "PENDING")]  # handed back, never silently consumed
    finally:
        _cleanup_heartbeats(worker_id)


# 11. NEEDS_INPUT does not block the next independent run item -- enforced
# by claim_next_queued_application_for_run() itself (only blocks on
# SESSION_LEVEL interventions, never a plain NEEDS_INPUT/no-intervention
# application from a different job in the same run).
def test_needs_input_application_does_not_block_next_run_item_claim():
    from db.application_repository import claim_next_queued_application_for_run

    job1, app1 = _make_job_and_application("TEST Daemon NeedsInput A")
    job2, app2 = _make_job_and_application("TEST Daemon NeedsInput B")
    worker_id = f"TEST-worker-{uuid.uuid4()}"
    try:
        run = run_registry.create_run([app1["id"], app2["id"]], candidate_id=CANDIDATE)

        update_application_status(app1["id"], "PROCESSING", worker_id=worker_id)
        update_application_status(app1["id"], "NEEDS_INPUT")  # awaiting-confirmation style, no open intervention

        claimed = claim_next_queued_application_for_run(run["id"], worker_id)
        assert claimed is not None
        assert claimed["id"] == app2["id"]  # second item claimable even though the first is NEEDS_INPUT
    finally:
        _cleanup(job1["id"], job2["id"])


# 12. STOP prevents the next item claim
def test_stop_requested_prevents_next_claim():
    from dice_browser.worker import run_worker_for_run

    job1, app1 = _make_job_and_application("TEST Daemon Stop A")
    job2, app2 = _make_job_and_application("TEST Daemon Stop B")
    worker_id = f"TEST-worker-{uuid.uuid4()}"
    try:
        run = run_registry.create_run([app1["id"], app2["id"]], candidate_id=CANDIDATE)
        run_registry.request_stop(run["id"])

        summary = run_worker_for_run(page=object(), run_id=run["id"], worker_id=worker_id)

        assert summary.processed == []  # stop was already requested before the first claim
        assert get_application(app1["id"])["status"] == "QUEUED"
        assert get_application(app2["id"])["status"] == "QUEUED"
        assert run_registry.get_run(run["id"])["status"] == "STOPPED"
    finally:
        _cleanup(job1["id"], job2["id"])


# 13. SUBMITTED persisted, and (14) no duplicate submit
def test_submitted_persisted_and_duplicate_submit_rejected():
    job, application = _make_job_and_application("TEST Daemon Submitted")
    try:
        update_application_status(application["id"], "PROCESSING")
        update_application_status(application["id"], "SUBMITTING")
        result = SubmissionResult(
            status=SubmissionStatus.VERIFIED_SUBMITTED,
            reason="explicit confirmation text found",
            evidence={"confirmation_text": "your application is on its way"},
            application_id=application["id"], dice_job_id=job["id"],
            before_url="https://dice.com/x/wizard", after_url="https://dice.com/x/wizard/success",
        )
        record_submission_result(application["id"], result)
        assert get_application(application["id"])["status"] == "SUBMITTED"

        with pytest.raises(InvalidStatusTransitionError):
            record_submission_result(application["id"], result)
    finally:
        _cleanup(job["id"])


# 15. Worker daemon can be re-invoked (restarted) safely and keeps polling
# correctly -- each invocation independently claims/heartbeats without
# corrupting state. (True mid-run crash recovery of an in-flight RUNNING
# run is a known gap, matching the same documented limitation already in
# dice_browser.worker for a single stuck PROCESSING application -- not
# claimed here.)
def test_daemon_restart_across_two_separate_invocations_is_safe(monkeypatch):
    worker_id_1 = f"TEST-worker-{uuid.uuid4()}"
    worker_id_2 = f"TEST-worker-{uuid.uuid4()}"
    queue = [
        {"id": "TEST-fake-run-restart-1", "candidate_id": CANDIDATE, "status": "RUNNING", "application_ids": [], "submission_policy": "REQUIRE_CONFIRMATION"},
        {"id": "TEST-fake-run-restart-2", "candidate_id": CANDIDATE, "status": "RUNNING", "application_ids": [], "submission_policy": "REQUIRE_CONFIRMATION"},
    ]

    def _fake_claim(wid):
        return queue.pop(0) if queue else None

    seen = []
    monkeypatch.setattr(run_registry, "claim_next_pending_run", _fake_claim)
    monkeypatch.setattr(worker_daemon, "run_worker_for_run", lambda page, run_id, *a, **kw: seen.append(run_id))
    monkeypatch.setattr(worker_daemon, "_connect", _fake_connect)
    try:
        worker_daemon.run_daemon(worker_id_1, max_iterations=1, poll_interval=0)  # "first process"
        worker_daemon.run_daemon(worker_id_2, max_iterations=1, poll_interval=0)  # "restarted process"

        assert seen == ["TEST-fake-run-restart-1", "TEST-fake-run-restart-2"]  # each claimed exactly once, across two independent starts
    finally:
        _cleanup_heartbeats(worker_id_1, worker_id_2)


# 16. No local-file dependency for production run state
def test_run_registry_has_no_local_file_dependency():
    import run_registry as rr

    source = inspect.getsource(rr)
    assert "open(" not in source
    assert ".json" not in source
    assert "RUNS_DIR" not in source
