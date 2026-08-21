"""Phase 5: DB-side submission recording. Runs against the same in-memory
fake Supabase client as the rest of this project's offline repository
tests (tests/conftest.py::fake_repo) -- no live project required.
"""
from __future__ import annotations

from db.application_repository import InvalidStatusTransitionError
from db.submission_repository import record_submission_result
from dice_browser.models import SubmissionResult, SubmissionStatus

CANDIDATE = "11111111-1111-1111-1111-111111111111"


def _make_submitting_application(fake_repo, dice_job_id="DICE-5-1"):
    job = fake_repo.upsert_dice_job(
        {"dice_job_id": dice_job_id, "canonical_url": f"https://dice.com/job/{dice_job_id}", "title": "Phase 5 Test Role"}
    )
    app = fake_repo.enqueue_application(CANDIDATE, job["id"])
    fake_repo.update_application_status(app["id"], "PROCESSING", worker_id="test-worker")
    fake_repo.update_application_status(app["id"], "SUBMITTING")
    return app


def _result(status: SubmissionStatus, evidence=None) -> SubmissionResult:
    return SubmissionResult(
        status=status,
        reason="test",
        evidence=evidence or {},
        application_id="app-1",
        dice_job_id="job-1",
        before_url="https://www.dice.com/job-applications/x/wizard",
        after_url="https://www.dice.com/applications/confirmation",
    )


# 16. application status transitions to SUBMITTED only after VERIFIED_SUBMITTED
def test_verified_submitted_transitions_application_status(fake_repo):
    app = _make_submitting_application(fake_repo)
    record_submission_result(app["id"], _result(SubmissionStatus.VERIFIED_SUBMITTED, {"confirmation_text": "Application submitted"}))

    updated = fake_repo.get_application(app["id"])
    assert updated["status"] == "SUBMITTED"
    assert updated["submitted_at"] is not None
    assert updated["verification_evidence"] == {"confirmation_text": "Application submitted"}


# 17. application event written capturing the classified result
def test_event_written_for_submission_result(fake_repo):
    app = _make_submitting_application(fake_repo)
    record_submission_result(app["id"], _result(SubmissionStatus.VERIFIED_SUBMITTED, {"confirmation_text": "Application submitted"}))

    client = fake_repo.get_supabase_client()
    events = [e for e in client.tables["application_events"] if e["application_id"] == app["id"]]
    submission_events = [e for e in events if e["event_type"] == "submission_result"]
    assert len(submission_events) == 1
    assert submission_events[0]["metadata"]["status"] == SubmissionStatus.VERIFIED_SUBMITTED


# 18. failure/uncertain outcome does not become SUBMITTED
def test_uncertain_outcome_does_not_transition_to_submitted(fake_repo):
    app = _make_submitting_application(fake_repo)
    record_submission_result(app["id"], _result(SubmissionStatus.VERIFICATION_UNCERTAIN))

    updated = fake_repo.get_application(app["id"])
    assert updated["status"] == "SUBMITTING"  # untouched


def test_not_submitted_outcome_does_not_transition_to_submitted(fake_repo):
    app = _make_submitting_application(fake_repo)
    record_submission_result(app["id"], _result(SubmissionStatus.NOT_SUBMITTED))

    updated = fake_repo.get_application(app["id"])
    assert updated["status"] == "SUBMITTING"


def test_event_still_written_for_uncertain_outcome(fake_repo):
    app = _make_submitting_application(fake_repo)
    record_submission_result(app["id"], _result(SubmissionStatus.VERIFICATION_UNCERTAIN))

    client = fake_repo.get_supabase_client()
    events = [e for e in client.tables["application_events"] if e["application_id"] == app["id"]]
    assert any(e["event_type"] == "submission_result" for e in events)


# 19. restart does not re-submit a verified application -- a second
# attempt to record success against an already-SUBMITTED application
# fails loudly (invalid transition) rather than silently succeeding again.
def test_duplicate_verified_result_on_already_submitted_application_raises(fake_repo):
    app = _make_submitting_application(fake_repo)
    record_submission_result(app["id"], _result(SubmissionStatus.VERIFIED_SUBMITTED))

    try:
        record_submission_result(app["id"], _result(SubmissionStatus.VERIFIED_SUBMITTED))
        assert False, "expected InvalidStatusTransitionError"
    except InvalidStatusTransitionError:
        pass

    # exactly one submitted_at was ever recorded
    client = fake_repo.get_supabase_client()
    submission_events = [
        e
        for e in client.tables["application_events"]
        if e["application_id"] == app["id"] and e["event_type"] == "submission_result"
    ]
    assert len(submission_events) == 2  # both attempts are logged -- the event log is honest even about the rejected retry
    assert fake_repo.get_application(app["id"])["status"] == "SUBMITTED"


# 20. no force retry after uncertain state -- nothing in this module
# escalates or retries on its own; two independent uncertain results
# leave status untouched both times.
def test_no_automatic_retry_or_escalation_on_repeated_uncertain_results(fake_repo):
    app = _make_submitting_application(fake_repo)
    record_submission_result(app["id"], _result(SubmissionStatus.VERIFICATION_UNCERTAIN))
    record_submission_result(app["id"], _result(SubmissionStatus.VERIFICATION_UNCERTAIN))

    assert fake_repo.get_application(app["id"])["status"] == "SUBMITTING"
