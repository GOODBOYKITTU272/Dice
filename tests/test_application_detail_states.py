"""Application Detail page -- state-specific display (QUEUED / RUNNING /
NEEDS_INPUT / AWAITING_SUBMIT_CONFIRMATION / SUBMITTED / FAILED). Real
Supabase, disposable TEST- rows, cleaned up per test. No worker/browser
code touched -- every state here is produced by direct status writes,
never a real Dice mutation.
"""
from __future__ import annotations

import re
import uuid

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
import run_registry

CANDIDATE = "88888888-8888-8888-8888-888888888888"


def _client():
    return app.test_client()


def _make_job_and_application(title, easy_apply=True):
    dice_job_id = f"TEST-{uuid.uuid4()}"
    job = upsert_dice_job(
        {"dice_job_id": dice_job_id, "canonical_url": f"https://dice.com/job/{dice_job_id}", "title": title, "is_easy_apply": easy_apply}
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


def _has_status_badge(body: str, label: str) -> bool:
    return re.search(rf'class="badge[^"]*">\s*{re.escape(label)}\s*</span>', body) is not None


def _timeline_state(body: str) -> dict[str, bool]:
    """Maps each rendered timeline step label to whether its <li> carries
    the "done" class -- lets a test assert exactly which steps are (and
    are not) marked complete."""
    return {
        label: 'class="done"' in li or "class=\"done \"" in li
        for li, label in re.findall(r'(<li class="([^"]*)"><span class="dot"></span>([^<]+)</li>)', body)
    }


# 1/2/3/10. QUEUED: banner, only "Job queued" done, explanatory no-events message
def test_queued_application_shows_queued_banner_and_only_job_queued_step_done():
    job, application = _make_job_and_application("TEST Detail Queued")
    try:
        body = _client().get(f"/applications/{application['id']}").get_data(as_text=True)
        assert "Queued for worker" in body
        assert "has not started yet" in body
        assert "Waiting for worker activity. Events will appear here once processing begins." in body
        assert "No events recorded." not in body

        # No fake progress: only "Job queued" is done; every later step
        # (claim, live qualification, auth, easy apply, ... submission
        # verified) must NOT be marked done for a job that never started.
        li_blocks = re.findall(r'<li class="([^"]*)"><span class="dot"></span>([^<]+)</li>', body)
        done_labels = {label.strip() for cls, label in li_blocks if "done" in cls}
        all_labels = {label.strip() for _, label in li_blocks}
        assert done_labels == {"Job queued"}
        assert all_labels - done_labels == {
            "Job claimed", "Live qualification", "Authentication", "Easy Apply opened", "Resume checked",
            "Questions checked", "Review reached", "Submit clicked", "Submission verified",
        }
    finally:
        _cleanup(job["id"])


# 4. RUNNING shows current step
def test_running_application_shows_current_step():
    job, application = _make_job_and_application("TEST Detail Running")
    try:
        update_application_status(application["id"], "PROCESSING")
        from db.application_repository import add_event

        add_event(application["id"], event_type="easy_apply_opened", step="CLICK_EASY_APPLY", message="OPENED")

        body = _client().get(f"/applications/{application['id']}").get_data(as_text=True)
        assert "Application in progress" in body
        assert "Easy Apply opened" in body
    finally:
        _cleanup(job["id"])


# 5. NEEDS_INPUT shows Answer Question
def test_needs_input_application_shows_answer_question():
    job, application = _make_job_and_application("TEST Detail NeedsInput")
    try:
        update_application_status(application["id"], "PROCESSING")
        create_intervention(
            application_id=application["id"],
            intervention_type="MISSING_CANDIDATE_FACT",
            intervention_scope="APPLICATION_LEVEL",
            question_text="What is your expected rate?",
            options={"question_id": "q-1", "field_type": "TEXTAREA", "reason": "no trusted mapping", "sensitivity": False, "choices": None},
        )

        body = _client().get(f"/applications/{application['id']}").get_data(as_text=True)
        assert "DicePilot needs your input" in body
        assert "1 question" in body
        assert "What is your expected rate?" in body
        assert "no trusted mapping" in body
        assert "Answer Question" in body
        assert "View Interventions" in body
    finally:
        _cleanup(job["id"])


# 6. AWAITING_SUBMIT_CONFIRMATION shows "Ready to submit"
def test_awaiting_submit_confirmation_shows_ready_to_submit():
    job, application = _make_job_and_application("TEST Detail Awaiting")
    try:
        update_application_status(application["id"], "PROCESSING")
        update_application_status(application["id"], "NEEDS_INPUT")  # no intervention -> awaiting confirmation

        body = _client().get(f"/applications/{application['id']}").get_data(as_text=True)
        assert "Ready to submit" in body
        assert "reached the final Review step" in body
        assert "Submission requires confirmation" in body
        assert _has_status_badge(body, "AWAITING_SUBMIT_CONFIRMATION")
    finally:
        _cleanup(job["id"])


# 7. SUBMITTED shows real verification evidence
def test_submitted_application_shows_real_verification_evidence():
    job, application = _make_job_and_application("TEST Detail Submitted")
    try:
        update_application_status(application["id"], "PROCESSING")
        update_application_status(application["id"], "SUBMITTING")
        result = SubmissionResult(
            status=SubmissionStatus.VERIFIED_SUBMITTED,
            reason="explicit confirmation text found and the page left the wizard",
            evidence={"confirmation_text": "your application is on its way", "before_url": "https://dice.com/x/wizard", "after_url": "https://dice.com/x/wizard/success"},
            application_id=application["id"], dice_job_id=job["id"],
            before_url="https://dice.com/x/wizard", after_url="https://dice.com/x/wizard/success",
        )
        record_submission_result(application["id"], result)

        body = _client().get(f"/applications/{application['id']}").get_data(as_text=True)
        assert "Submission positively verified by Dice" in body
        assert "VERIFIED_SUBMITTED" in body
        assert "your application is on its way" in body
        assert "https://dice.com/x/wizard/success" in body
        assert _has_status_badge(body, "SUBMITTED")
    finally:
        _cleanup(job["id"])


# 8. FAILED shows exact failure reason, not just "Error"
def test_failed_application_shows_exact_reason():
    job, application = _make_job_and_application("TEST Detail Failed")
    try:
        update_application_status(application["id"], "PROCESSING")
        update_application_status(application["id"], "FAILED", error_code="SECURITY_CHALLENGE", error_message="captcha shown")

        body = _client().get(f"/applications/{application['id']}").get_data(as_text=True)
        assert "Application stopped" in body
        assert "Dice security challenge detected. Human action required." in body
        assert body.count(">Error<") == 0
    finally:
        _cleanup(job["id"])


# 9. Run link renders when run_id exists (Overview + banner both link to Run Progress)
def test_run_link_renders_when_application_belongs_to_a_run():
    job, application = _make_job_and_application("TEST Detail RunLink")
    run = run_registry.create_run([application["id"]], candidate_id=CANDIDATE)
    try:
        body = _client().get(f"/applications/{application['id']}").get_data(as_text=True)
        assert f"/runs/{run['id']}" in body
        assert "Batch Position" in body
        assert "1 of 1" in body
        assert "Submission Policy" in body
        assert "REQUIRE_CONFIRMATION" in body
    finally:
        _cleanup(job["id"])


def test_no_run_link_when_application_has_no_run():
    job, application = _make_job_and_application("TEST Detail NoRun")
    try:
        body = _client().get(f"/applications/{application['id']}").get_data(as_text=True)
        assert "Batch Position" not in body
        assert "/runs/" not in body
    finally:
        _cleanup(job["id"])
