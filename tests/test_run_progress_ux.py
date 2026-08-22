"""UX alignment: the Review & Apply button actually starts the worker
(not just queues), Run Progress shows the real REQUIRE_CONFIRMATION
vocabulary (AWAITING_SUBMIT_CONFIRMATION distinct from NEEDS_INPUT), and
the obsolete "process via Worker page / CLI" banner is gone. Real
Supabase, disposable TEST- rows, subprocess.Popen mocked throughout (no
real worker, no real Dice mutation).
"""
from __future__ import annotations

import re
import uuid

import pytest

import local_app.app as app_module
import run_registry
from db.application_repository import (
    create_intervention,
    enqueue_application,
    get_supabase_client,
    update_application_status,
    upsert_dice_job,
)
from db.submission_repository import record_submission_result
from dice_browser.models import SubmissionResult, SubmissionStatus
from local_app.app import app

TEST_CANDIDATE_ID = "77777777-7777-7777-7777-777777777777"


@pytest.fixture(autouse=True)
def _no_real_worker(monkeypatch):
    monkeypatch.setattr(app_module.subprocess, "Popen", lambda *a, **k: None)
    monkeypatch.setenv("DICEPILOT_CANDIDATE_ID", TEST_CANDIDATE_ID)


def _client():
    return app.test_client()


def _make_job(title, easy_apply=True):
    dice_job_id = f"TEST-{uuid.uuid4()}"
    return upsert_dice_job(
        {"dice_job_id": dice_job_id, "canonical_url": f"https://dice.com/job/{dice_job_id}", "title": title, "is_easy_apply": easy_apply}
    )


def _has_status_badge(body: str, label: str) -> bool:
    return re.search(rf'class="badge[^"]*">\s*{re.escape(label)}\s*</span>', body) is not None


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


# 1. The Review & Apply page's final button says "Start Applications" and its
#    copy no longer claims it "only queues".
def test_review_page_button_says_start_applications():
    job = _make_job("TEST UX Review Button")
    try:
        body = _client().post("/jobs/review", data={"job_id": [job["id"]]}).get_data(as_text=True)
        assert "Start Applications" in body
        assert "Apply to Selected Jobs" not in body
        assert "does not run any application itself" not in body
        assert "DicePilot will process these selected jobs one at a time." in body
    finally:
        _cleanup(job["id"])


# 4. Applications page no longer shows the obsolete "process via Worker / CLI" banner
def test_applications_page_has_no_obsolete_cli_banner():
    body = _client().get("/applications").get_data(as_text=True)
    assert "process them via the Worker page / CLI" not in body


def test_no_eligible_jobs_shows_clear_error_not_obsolete_banner():
    job = _make_job("TEST UX No Eligible", easy_apply=False)  # SKIPPED, not selectable
    try:
        resp = _client().post("/jobs/apply", data={"job_id": [job["id"]]}, follow_redirects=True)
        body = resp.get_data(as_text=True)
        assert "process them via the Worker page / CLI" not in body
        assert "select different jobs" in body.lower()
    finally:
        _cleanup(job["id"])


# 5/6. Run Progress initially shows selected jobs, current job/status
def test_run_progress_shows_run_status_and_selected_jobs_immediately():
    job = _make_job("TEST UX Run Status")
    try:
        resp = _client().post("/jobs/apply", data={"job_id": [job["id"]]}, follow_redirects=False)
        run_id = resp.headers["Location"].rsplit("/", 1)[-1]

        body = _client().get(f"/runs/{run_id}").get_data(as_text=True)
        assert "Run Status:" in body
        assert "TEST UX Run Status" in body
        assert "Selected" in body
    finally:
        _cleanup(job["id"])


# 7. REQUIRE_CONFIRMATION -> AWAITING_SUBMIT_CONFIRMATION displays as "Ready to Submit" + "Review & Submit"
def test_run_progress_shows_awaiting_submit_confirmation_as_ready_to_submit():
    job = _make_job("TEST UX Awaiting Confirm")
    app_ = enqueue_application(TEST_CANDIDATE_ID, job["id"])
    update_application_status(app_["id"], "PROCESSING")
    update_application_status(app_["id"], "NEEDS_INPUT")  # no intervention -> awaiting confirmation
    run = run_registry.create_run([app_["id"]], candidate_id=TEST_CANDIDATE_ID)
    try:
        body = _client().get(f"/runs/{run['id']}").get_data(as_text=True)
        assert "AWAITING_SUBMIT_CONFIRMATION" in body
        assert "Ready to Submit" in body
        assert "Review &amp; Submit" in body or "Review & Submit" in body
    finally:
        _cleanup(job["id"])


# 8. NEEDS_INPUT (real question) displays "Needs Input" + "Answer Question"
def test_run_progress_shows_needs_input_with_answer_question_action():
    job = _make_job("TEST UX Needs Input")
    app_ = enqueue_application(TEST_CANDIDATE_ID, job["id"])
    update_application_status(app_["id"], "PROCESSING")
    create_intervention(
        application_id=app_["id"],
        intervention_type="MISSING_CANDIDATE_FACT",
        intervention_scope="APPLICATION_LEVEL",
        question_text="Expected salary?",
        options={"question_id": "q-1", "field_type": "TEXTAREA", "reason": "no trusted mapping", "sensitivity": False, "choices": None},
    )
    run = run_registry.create_run([app_["id"]], candidate_id=TEST_CANDIDATE_ID)
    try:
        body = _client().get(f"/runs/{run['id']}").get_data(as_text=True)
        assert _has_status_badge(body, "NEEDS_INPUT")
        assert "Answer Question" in body
    finally:
        _cleanup(job["id"])


# 9. SUBMITTED displays a verification link
def test_run_progress_shows_submitted_with_verification_link():
    job = _make_job("TEST UX Submitted")
    app_ = enqueue_application(TEST_CANDIDATE_ID, job["id"])
    update_application_status(app_["id"], "PROCESSING")
    update_application_status(app_["id"], "SUBMITTING")
    result = SubmissionResult(
        status=SubmissionStatus.VERIFIED_SUBMITTED,
        reason="explicit confirmation text found and the page left the wizard",
        evidence={"confirmation_text": "on its way", "before_url": "https://dice.com/x/wizard", "after_url": "https://dice.com/x/wizard/success"},
        application_id=app_["id"], dice_job_id=job["id"],
        before_url="https://dice.com/x/wizard", after_url="https://dice.com/x/wizard/success",
    )
    record_submission_result(app_["id"], result)
    run = run_registry.create_run([app_["id"]], candidate_id=TEST_CANDIDATE_ID)
    try:
        body = _client().get(f"/runs/{run['id']}").get_data(as_text=True)
        assert _has_status_badge(body, "SUBMITTED")
        assert "View Verification" in body
    finally:
        _cleanup(job["id"])


# 10. FAILED displays the exact failure reason
def test_run_progress_shows_failed_with_exact_reason():
    job = _make_job("TEST UX Failed")
    app_ = enqueue_application(TEST_CANDIDATE_ID, job["id"])
    update_application_status(app_["id"], "PROCESSING")
    update_application_status(app_["id"], "FAILED", error_code="SUBMIT_FAILED", error_message="Dice reported a failure")
    run = run_registry.create_run([app_["id"]], candidate_id=TEST_CANDIDATE_ID)
    try:
        body = _client().get(f"/runs/{run['id']}").get_data(as_text=True)
        assert _has_status_badge(body, "FAILED")
        assert "Dice explicitly reported that the application could not be submitted." in body
    finally:
        _cleanup(job["id"])


# 11. STOPPED shows a real Resume Run button, not just "Run finished"
def test_run_progress_shows_resume_run_button_for_stopped_run():
    job = _make_job("TEST UX Stopped")
    app_ = enqueue_application(TEST_CANDIDATE_ID, job["id"])
    run = run_registry.create_run([app_["id"]], candidate_id=TEST_CANDIDATE_ID)
    run_registry.update_run_status(run["id"], "STOPPED")
    try:
        body = _client().get(f"/runs/{run['id']}").get_data(as_text=True)
        assert "Resume Run" in body
        assert f'action="/runs/{run["id"]}/resume"' in body
    finally:
        _cleanup(job["id"])


# 12. Worker-offline copy is truthful: queued work will start automatically,
# no terminal instruction shown to the normal user. worker_status() is
# monkeypatched -- this project's own real daemon may genuinely be online
# in the shared Supabase project while this test runs, so the real,
# global (not worker_id-scoped -- a documented limitation) heartbeat
# state can't be relied on to be offline here.
def test_run_progress_worker_offline_message_says_automatic_not_terminal(monkeypatch):
    monkeypatch.setattr(
        run_registry, "worker_status", lambda max_age_seconds=30: {"online": False, "status": "OFFLINE", "last_heartbeat_at": None, "age_seconds": None}
    )
    job = _make_job("TEST UX Offline")
    app_ = enqueue_application(TEST_CANDIDATE_ID, job["id"])
    run = run_registry.create_run([app_["id"]], candidate_id=TEST_CANDIDATE_ID)
    try:
        body = _client().get(f"/runs/{run['id']}").get_data(as_text=True)
        assert "DicePilot worker is offline. Applications are queued and will start automatically when the worker reconnects." in body
        assert "worker_daemon" not in body  # no CLI/module instruction shown in the normal UI
    finally:
        _cleanup(job["id"])
