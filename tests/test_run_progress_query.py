"""local_app.queries.run_progress -- counters, current job, and the
intervention/verification links the Run Progress page depends on. Real
Supabase, real status transitions, disposable TEST- rows cleaned up per
test (matching this project's convention).
"""
from __future__ import annotations

import uuid

from db.application_repository import (
    add_event,
    create_intervention,
    enqueue_application,
    get_supabase_client,
    update_application_status,
    upsert_dice_job,
)
from db.submission_repository import record_submission_result
from dice_browser.models import SubmissionResult, SubmissionStatus
from local_app import queries

CANDIDATE = "33333333-3333-3333-3333-333333333333"


def _client():
    return get_supabase_client()


def _make_job_and_application(title):
    dice_job_id = f"TEST-{uuid.uuid4()}"
    job = upsert_dice_job(
        {"dice_job_id": dice_job_id, "canonical_url": f"https://dice.com/job/{dice_job_id}", "title": title}
    )
    application = enqueue_application(CANDIDATE, job["id"])
    return job, application


def _cleanup(*job_ids: str):
    sc = _client()
    for job_id in job_ids:
        apps = sc.table("applications").select("id").eq("dice_job_id", job_id).execute().data
        for a in apps:
            sc.table("interventions").delete().eq("application_id", a["id"]).execute()
            sc.table("application_events").delete().eq("application_id", a["id"]).execute()
            sc.table("applications").delete().eq("id", a["id"]).execute()
        sc.table("dice_jobs").delete().eq("id", job_id).execute()


# 10/11/12/13/14: counters reflect a genuine mix of statuses
def test_run_progress_counters_reflect_mixed_statuses():
    job_submitted, app_submitted = _make_job_and_application("TEST Progress Submitted")
    job_needs_input, app_needs_input = _make_job_and_application("TEST Progress NeedsInput")
    job_failed, app_failed = _make_job_and_application("TEST Progress Failed")
    job_running, app_running = _make_job_and_application("TEST Progress Running")
    job_queued, app_queued = _make_job_and_application("TEST Progress Queued")
    try:
        update_application_status(app_submitted["id"], "PROCESSING")
        update_application_status(app_submitted["id"], "SUBMITTING")
        update_application_status(app_submitted["id"], "SUBMITTED", submitted_at="2026-08-22T00:00:00Z")

        update_application_status(app_needs_input["id"], "PROCESSING")
        update_application_status(app_needs_input["id"], "NEEDS_INPUT")

        update_application_status(app_failed["id"], "PROCESSING")
        update_application_status(app_failed["id"], "FAILED", error_code="TEST", error_message="boom")

        update_application_status(app_running["id"], "PROCESSING")

        run = {
            "id": "test-run",
            "candidate_id": CANDIDATE,
            "status": "RUNNING",
            "application_ids": [
                app_submitted["id"], app_needs_input["id"], app_failed["id"], app_running["id"], app_queued["id"],
            ],
        }
        progress = queries.run_progress(_client(), run)

        assert progress["counts"]["selected"] == 5
        assert progress["counts"]["submitted"] == 1
        assert progress["counts"]["needs_input"] == 1
        assert progress["counts"]["failed"] == 1
        assert progress["counts"]["running"] == 1
        assert progress["counts"]["remaining"] == 1  # only the still-QUEUED one
        assert progress["counts"]["processed"] == 3  # submitted + needs_input + failed
    finally:
        _cleanup(job_submitted["id"], job_needs_input["id"], job_failed["id"], job_running["id"], job_queued["id"])


# "current job visible" + current_step_label derives from the latest event
def test_run_progress_current_job_reflects_latest_event_step():
    job, application = _make_job_and_application("TEST Progress Current Step")
    try:
        update_application_status(application["id"], "PROCESSING")
        add_event(application["id"], event_type="easy_apply_opened", step="CLICK_EASY_APPLY", message="OPENED")

        run = {"id": "test-run", "candidate_id": CANDIDATE, "status": "RUNNING", "application_ids": [application["id"]]}
        progress = queries.run_progress(_client(), run)

        assert progress["current"] is not None
        assert progress["current"]["id"] == application["id"]
        assert progress["current"]["current_step_label"] == "Easy Apply opened"
    finally:
        _cleanup(job["id"])


# 16. intervention link works -- open_intervention_id populated for a NEEDS_INPUT row
def test_run_progress_needs_input_row_links_to_its_open_intervention():
    job, application = _make_job_and_application("TEST Progress Intervention Link")
    try:
        update_application_status(application["id"], "PROCESSING")
        intervention = create_intervention(
            application_id=application["id"],
            intervention_type="MISSING_CANDIDATE_FACT",
            intervention_scope="APPLICATION_LEVEL",
            question_text="Expected salary?",
            options={"question_id": "q-1", "field_type": "TEXTAREA", "reason": "no trusted mapping", "sensitivity": False, "choices": None},
        )

        run = {"id": "test-run", "candidate_id": CANDIDATE, "status": "RUNNING", "application_ids": [application["id"]]}
        progress = queries.run_progress(_client(), run)

        row = progress["rows"][0]
        assert row["status"] == "NEEDS_INPUT"
        assert row["open_intervention_id"] == intervention["id"]
    finally:
        _cleanup(job["id"])


# 17. submitted application row carries its verification evidence for the "View Verification" link
def test_run_progress_submitted_row_has_verification_evidence():
    job, application = _make_job_and_application("TEST Progress Verification Evidence")
    try:
        update_application_status(application["id"], "PROCESSING")
        update_application_status(application["id"], "SUBMITTING")
        result = SubmissionResult(
            status=SubmissionStatus.VERIFIED_SUBMITTED,
            reason="explicit confirmation text found and the page left the wizard",
            evidence={"confirmation_text": "on its way", "before_url": "https://dice.com/x/wizard", "after_url": "https://dice.com/x/wizard/success"},
            application_id=application["id"], dice_job_id=job["id"],
            before_url="https://dice.com/x/wizard", after_url="https://dice.com/x/wizard/success",
        )
        record_submission_result(application["id"], result)

        run = {"id": "test-run", "candidate_id": CANDIDATE, "status": "RUNNING", "application_ids": [application["id"]]}
        progress = queries.run_progress(_client(), run)

        row = progress["rows"][0]
        assert row["status"] == "SUBMITTED"
        assert row["verification_evidence"]["confirmation_text"] == "on its way"
    finally:
        _cleanup(job["id"])
