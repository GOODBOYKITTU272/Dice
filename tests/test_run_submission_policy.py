"""Phase 6.4: submission policy belongs to the RUN, persisted at creation
time -- never a CLI-wide daemon default. Covers the 16 explicit
requirements from the "auto-submit run policy" task:

 1. normal Start Applications creates AUTHORIZED_AUTONOMOUS run
 2. submission policy persisted in Supabase
 3. daemon reads policy from the run
 4. daemon does not require a CLI submission-policy flag
 5. autonomous run submits after all gates (worker.py-level coverage:
    tests/test_worker.py::test_process_one_application_authorized_autonomous_submits;
    here we cover the daemon->worker plumbing that selects that policy)
 6. confirmation run stops at Review (same split: worker.py-level
    coverage already exists; here, the daemon-level policy selection)
 7. one daemon can process runs with different policies correctly
 8. existing run policy is immutable after creation
 9. Resume Run preserves original policy
10. Settings change does not mutate an existing run
11. exact selected-run scoping preserved (existing coverage:
    tests/test_worker_daemon_architecture.py, tests/test_jobs_apply_to_worker.py --
    unaffected by this change, not duplicated here)
12. maximum one Submit click (existing coverage: dice_browser/submission.py's
    own no-retry design, tests/test_dice_browser_submission.py -- unchanged)
13. NEEDS_INPUT never guessed (existing coverage: _walk_questions_to_review,
    tests/test_worker.py -- unchanged)
14. VERIFICATION_UNCERTAIN never auto-retried (existing coverage:
    tests/test_worker.py's AUTHORIZED_AUTONOMOUS tests -- unchanged)
15. Run Progress / Review & Apply display human-readable submission mode
16. no real Dice mutation in any test in this file (daemon-level tests
    mock claim_next_pending_run/_connect/run_worker_for_run entirely --
    same discipline as test_worker_daemon_architecture.py, and for the
    same reason: claim_next_pending_run's real RPC has no test isolation
    from this shared project's real run rows)
"""
from __future__ import annotations

import uuid

import pytest

import dice_browser.worker_daemon as worker_daemon
import local_app.app as app_module
import run_registry
from db.application_repository import enqueue_application, get_supabase_client, upsert_dice_job
from dice_browser.worker import SubmissionPolicy
from local_app.app import app

CANDIDATE = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


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


# 1 & 2. Normal Start Applications creates an AUTHORIZED_AUTONOMOUS run,
# persisted in Supabase (DICEPILOT_SUBMISSION_MODE unset -- the default).
def test_start_applications_creates_autonomous_run_by_default(monkeypatch):
    monkeypatch.setenv("DICEPILOT_CANDIDATE_ID", CANDIDATE)
    monkeypatch.delenv("DICEPILOT_SUBMISSION_MODE", raising=False)
    job = _make_job_only("TEST Policy Default")
    try:
        resp = _client().post("/jobs/apply", data={"job_id": [job["id"]]}, follow_redirects=False)
        run_id = resp.headers["Location"].rsplit("/", 1)[-1]

        run = run_registry.get_run(run_id)
        assert run["submission_policy"] == "AUTHORIZED_AUTONOMOUS"

        row = get_supabase_client().table("application_runs").select("submission_policy").eq("id", run_id).execute().data[0]
        assert row["submission_policy"] == "AUTHORIZED_AUTONOMOUS"
    finally:
        _cleanup(job["id"])


def test_start_applications_honors_require_confirmation_override(monkeypatch):
    monkeypatch.setenv("DICEPILOT_CANDIDATE_ID", CANDIDATE)
    monkeypatch.setenv("DICEPILOT_SUBMISSION_MODE", "REQUIRE_CONFIRMATION")
    job = _make_job_only("TEST Policy Override")
    try:
        resp = _client().post("/jobs/apply", data={"job_id": [job["id"]]}, follow_redirects=False)
        run_id = resp.headers["Location"].rsplit("/", 1)[-1]
        assert run_registry.get_run(run_id)["submission_policy"] == "REQUIRE_CONFIRMATION"
    finally:
        _cleanup(job["id"])


# 3, 4, 5, 6. The daemon reads each run's own persisted policy -- no CLI
# flag required, no daemon-wide default. claim_next_pending_run and
# run_worker_for_run are both monkeypatched (see module docstring, item
# 16, for why this must never touch the shared live run queue).
def test_daemon_uses_autonomous_run_own_policy_with_no_cli_override(monkeypatch):
    worker_id = f"TEST-worker-{uuid.uuid4()}"
    fake_run = {"id": "TEST-fake-run-auto", "candidate_id": CANDIDATE, "submission_policy": "AUTHORIZED_AUTONOMOUS", "application_ids": []}
    captured = {}

    monkeypatch.setattr(run_registry, "claim_next_pending_run", lambda wid: fake_run)
    monkeypatch.setattr(worker_daemon, "run_worker_for_run", lambda page, run_id, wid, submission_policy, resume_path: captured.update(policy=submission_policy))
    monkeypatch.setattr(worker_daemon, "_connect", _fake_connect)
    try:
        # No submission_policy_override passed -- normal production call shape.
        worker_daemon.run_daemon(worker_id, max_iterations=1, poll_interval=0)
        assert captured["policy"] == SubmissionPolicy.AUTHORIZED_AUTONOMOUS
    finally:
        get_supabase_client().table("worker_heartbeats").delete().eq("worker_id", worker_id).execute()


def test_daemon_uses_confirmation_run_own_policy_with_no_cli_override(monkeypatch):
    worker_id = f"TEST-worker-{uuid.uuid4()}"
    fake_run = {"id": "TEST-fake-run-confirm", "candidate_id": CANDIDATE, "submission_policy": "REQUIRE_CONFIRMATION", "application_ids": []}
    captured = {}

    monkeypatch.setattr(run_registry, "claim_next_pending_run", lambda wid: fake_run)
    monkeypatch.setattr(worker_daemon, "run_worker_for_run", lambda page, run_id, wid, submission_policy, resume_path: captured.update(policy=submission_policy))
    monkeypatch.setattr(worker_daemon, "_connect", _fake_connect)
    try:
        worker_daemon.run_daemon(worker_id, max_iterations=1, poll_interval=0)
        assert captured["policy"] == SubmissionPolicy.REQUIRE_CONFIRMATION
    finally:
        get_supabase_client().table("worker_heartbeats").delete().eq("worker_id", worker_id).execute()


# 7. One daemon invocation correctly processes runs with different
# policies -- each with ITS OWN stored value, not a shared default.
def test_daemon_processes_mixed_policy_runs_correctly_in_one_invocation(monkeypatch):
    worker_id = f"TEST-worker-{uuid.uuid4()}"
    queue = [
        {"id": "TEST-fake-run-mixed-1", "candidate_id": CANDIDATE, "submission_policy": "AUTHORIZED_AUTONOMOUS", "application_ids": []},
        {"id": "TEST-fake-run-mixed-2", "candidate_id": CANDIDATE, "submission_policy": "REQUIRE_CONFIRMATION", "application_ids": []},
    ]

    def _fake_claim(wid):
        return queue.pop(0) if queue else None

    seen = []

    def _fake_run_worker_for_run(page, run_id, wid, submission_policy, resume_path):
        seen.append((run_id, submission_policy))

    monkeypatch.setattr(run_registry, "claim_next_pending_run", _fake_claim)
    monkeypatch.setattr(worker_daemon, "run_worker_for_run", _fake_run_worker_for_run)
    monkeypatch.setattr(worker_daemon, "_connect", _fake_connect)
    try:
        worker_daemon.run_daemon(worker_id, max_iterations=2, poll_interval=0)
        assert seen == [
            ("TEST-fake-run-mixed-1", SubmissionPolicy.AUTHORIZED_AUTONOMOUS),
            ("TEST-fake-run-mixed-2", SubmissionPolicy.REQUIRE_CONFIRMATION),
        ]
    finally:
        get_supabase_client().table("worker_heartbeats").delete().eq("worker_id", worker_id).execute()


def test_daemon_cli_override_applies_to_every_claimed_run_debug_only(monkeypatch):
    # The override exists only for explicit debug use -- when given, it
    # DOES apply uniformly (that's its whole purpose), unlike the normal,
    # no-override path which reads each run's own stored value.
    worker_id = f"TEST-worker-{uuid.uuid4()}"
    queue = [
        {"id": "TEST-fake-run-ov-1", "candidate_id": CANDIDATE, "submission_policy": "REQUIRE_CONFIRMATION", "application_ids": []},
        {"id": "TEST-fake-run-ov-2", "candidate_id": CANDIDATE, "submission_policy": "REQUIRE_CONFIRMATION", "application_ids": []},
    ]

    def _fake_claim(wid):
        return queue.pop(0) if queue else None

    seen = []
    monkeypatch.setattr(run_registry, "claim_next_pending_run", _fake_claim)
    monkeypatch.setattr(worker_daemon, "run_worker_for_run", lambda page, run_id, wid, submission_policy, resume_path: seen.append(submission_policy))
    monkeypatch.setattr(worker_daemon, "_connect", _fake_connect)
    try:
        worker_daemon.run_daemon(
            worker_id, max_iterations=2, poll_interval=0, submission_policy_override=SubmissionPolicy.AUTHORIZED_AUTONOMOUS
        )
        assert seen == [SubmissionPolicy.AUTHORIZED_AUTONOMOUS, SubmissionPolicy.AUTHORIZED_AUTONOMOUS]
    finally:
        get_supabase_client().table("worker_heartbeats").delete().eq("worker_id", worker_id).execute()


def test_daemon_cli_flag_defaults_to_no_override():
    parser = worker_daemon._build_arg_parser()
    args = parser.parse_args([])
    assert args.submission_policy is None


# 8 & 9. A run's policy is set once at creation and never changes --
# Resume Run in particular must not "upgrade" a REQUIRE_CONFIRMATION run.
def test_run_policy_immutable_after_creation():
    job, app_ = _make_job_and_application("TEST Policy Immutable")
    run = run_registry.create_run([app_["id"]], candidate_id=CANDIDATE, submission_policy="REQUIRE_CONFIRMATION")
    try:
        run_registry.update_run_status(run["id"], "STOPPED")
        assert run_registry.get_run(run["id"])["submission_policy"] == "REQUIRE_CONFIRMATION"
    finally:
        _cleanup(job["id"])


def test_resume_run_preserves_original_submission_policy():
    job, app_ = _make_job_and_application("TEST Resume Preserves Policy")
    run = run_registry.create_run([app_["id"]], candidate_id=CANDIDATE, submission_policy="REQUIRE_CONFIRMATION")
    try:
        run_registry.update_run_status(run["id"], "STOPPED")
        resumed = run_registry.resume_run(run["id"])
        assert resumed["submission_policy"] == "REQUIRE_CONFIRMATION"
    finally:
        _cleanup(job["id"])


# 10. Changing the env var after a run is created never mutates that run.
def test_settings_change_does_not_mutate_existing_run(monkeypatch):
    monkeypatch.setenv("DICEPILOT_CANDIDATE_ID", CANDIDATE)
    monkeypatch.delenv("DICEPILOT_SUBMISSION_MODE", raising=False)
    job = _make_job_only("TEST Policy Settings Immutable")
    try:
        resp = _client().post("/jobs/apply", data={"job_id": [job["id"]]}, follow_redirects=False)
        run_id = resp.headers["Location"].rsplit("/", 1)[-1]
        assert run_registry.get_run(run_id)["submission_policy"] == "AUTHORIZED_AUTONOMOUS"

        monkeypatch.setenv("DICEPILOT_SUBMISSION_MODE", "REQUIRE_CONFIRMATION")  # "Settings" changes after the fact
        assert run_registry.get_run(run_id)["submission_policy"] == "AUTHORIZED_AUTONOMOUS"  # unchanged
    finally:
        _cleanup(job["id"])


# 15. Human-readable submission mode displayed on Review & Apply and Run Progress.
def test_review_and_apply_shows_auto_submit_mode(monkeypatch):
    monkeypatch.setenv("DICEPILOT_CANDIDATE_ID", CANDIDATE)
    monkeypatch.delenv("DICEPILOT_SUBMISSION_MODE", raising=False)
    job = _make_job_only("TEST Policy Review Display")
    try:
        body = _client().post("/jobs/review", data={"job_id": [job["id"]]}).get_data(as_text=True)
        assert "Auto Submit" in body
        assert "DicePilot will submit eligible applications automatically after all safety checks pass." in body
    finally:
        _cleanup(job["id"])


def test_run_progress_shows_human_readable_submission_mode():
    job, app_ = _make_job_and_application("TEST Policy Run Progress Display")
    run = run_registry.create_run([app_["id"]], candidate_id=CANDIDATE, submission_policy="AUTHORIZED_AUTONOMOUS")
    try:
        body = _client().get(f"/runs/{run['id']}").get_data(as_text=True)
        assert "Auto Submit" in body
        assert "AUTHORIZED_AUTONOMOUS" not in body  # human label, not the raw enum
    finally:
        _cleanup(job["id"])
