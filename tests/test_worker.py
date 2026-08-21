"""Phase 6: dice_browser/worker.py -- the orchestration state machine.

This module's job is sequencing already-tested capability (Phase 2-5's
own extensive test suites already cover DOM extraction/classification
correctness); these tests mock the browser-layer functions worker.py
calls (monkeypatched at dice_browser.worker's own module namespace,
matching how every prior phase's fake_repo fixture patches
get_supabase_client) and exercise the real DB layer via
tests/conftest.py::fake_intervention_repo, so the actual state
transitions/idempotency/dedupe get verified against real logic, not a
second mock.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright

import db.application_repository as app_repo
import dice_browser.worker as worker
from dice.models import CandidateFetchResult, CandidateFetchStatus, CandidateProfile
from dice_browser.models import (
    BrowserState,
    ChallengeType,
    EasyApplyOpenResult,
    FieldType,
    NavigationResult,
    QuestionExtractionResult,
    QuestionExtractionStatus,
    QuestionField,
    QuestionStatus,
    RequiredState,
    ResumeUploadResult,
    SubmissionResult,
    SubmissionStatus,
)

CANDIDATE = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture()
def page(browser):
    pg = browser.new_page()
    pg.set_content("<html><body></body></html>")
    yield pg
    pg.close()


def _make_queued_application(dice_job_id="DICE-6-1"):
    job = app_repo.upsert_dice_job(
        {"dice_job_id": dice_job_id, "canonical_url": f"https://dice.com/job-detail/{dice_job_id}", "title": "Worker Test Role"}
    )
    return app_repo.enqueue_application(CANDIDATE, job["id"])


def _nav_result(authenticated=True, already_applied=False, easy_apply_visible=True, challenge=None):
    return NavigationResult(
        canonical_url="https://dice.com/job-detail/DICE-6-1",
        page_title="Worker Test Role",
        browser_state=BrowserState.ACTIVE if authenticated else BrowserState.AUTH_REQUIRED,
        authenticated=authenticated,
        already_applied=already_applied,
        easy_apply_visible=easy_apply_visible,
        challenge_type=challenge,
        evidence="test",
    )


def _no_questions_extraction():
    return QuestionExtractionResult(status=QuestionExtractionStatus.NO_QUESTIONS_PRESENT, questions=())


def _one_needs_input_question():
    q = QuestionField(
        question_id="onsite-q-uuid", prompt="Are you able and willing to regularly come into the office to work?",
        field_type=FieldType.RADIO, required_state=RequiredState.UNKNOWN, options=("Yes", "No"),
        current_value=None, helper=None, status=QuestionStatus.NEEDS_INPUT,
    )
    return QuestionExtractionResult(status=QuestionExtractionStatus.QUESTIONS_PRESENT, questions=(q,))


def _patch_happy_path(monkeypatch, questions_extraction=None, total_steps=None):
    # Modeled as an explicit step counter rather than a call-count fake --
    # a bare "Nth call returns True" fake can't represent the real
    # conditional (a 2-step no-question wizard reaches Review after one
    # click; a 3-step wizard with a question needs two). total_steps
    # defaults to 2 (Resume -> Review) when there are no questions, 3
    # (Resume -> Questions -> Review) when there are, matching every
    # real live shape observed so far.
    if total_steps is None:
        total_steps = 2 if questions_extraction is None else 3
    state = {"step": 1}

    monkeypatch.setattr(worker, "open_job", lambda page, url: _nav_result())
    monkeypatch.setattr(worker, "open_easy_apply", lambda page, nav: EasyApplyOpenResult(True, "url", "title", "OPENED"))
    monkeypatch.setattr(worker, "detect_existing_resume", lambda page: True)
    monkeypatch.setattr(worker, "is_review_screen", lambda page: state["step"] >= total_steps)
    monkeypatch.setattr(worker, "extract_questions", lambda page: questions_extraction or _no_questions_extraction())

    def fake_click_next(page):
        state["step"] += 1
        return True

    monkeypatch.setattr(worker, "click_next", fake_click_next)
    monkeypatch.setattr(
        worker,
        "fetch_candidate",
        lambda candidate_id: CandidateFetchResult(
            CandidateFetchStatus.SUCCESS,
            CandidateProfile(
                candidate_id=candidate_id, name="Jordan Rivera", email="jordan@example.com", phone=None,
                location=None, visa_type=None, work_authorized=None, requires_sponsorship=None,
                willing_to_relocate=None, experience_years=None, desired_start_date=None,
                resume_url=None, linkedin_url=None, github_url=None,
            ),
            None,
        ),
    )


# 1. Nothing queued
def test_process_one_application_nothing_queued(fake_intervention_repo, page, monkeypatch):
    result = worker.process_one_application(page, CANDIDATE, "test-worker")
    assert result.stop_reason == worker.StopReason.NOTHING_QUEUED


# 2. Happy path, no questions -> AWAITING_SUBMIT_CONFIRMATION (default policy)
def test_process_one_application_reaches_review_awaits_confirmation(fake_intervention_repo, page, monkeypatch):
    app = _make_queued_application()
    _patch_happy_path(monkeypatch)

    result = worker.process_one_application(page, CANDIDATE, "test-worker")
    assert result.application_id == app["id"]
    assert result.stop_reason == worker.StopReason.AWAITING_SUBMIT_CONFIRMATION

    events = [e for e in app_repo.get_supabase_client().tables["application_events"] if e["application_id"] == app["id"]]
    assert any(e["event_type"] == "awaiting_submit_confirmation" for e in events)


# 3. AUTH_REQUIRED on live re-check stops safely, marks FAILED_RETRYABLE
def test_process_one_application_auth_required_stops(fake_intervention_repo, page, monkeypatch):
    app = _make_queued_application()
    monkeypatch.setattr(worker, "open_job", lambda page, url: _nav_result(authenticated=False))

    result = worker.process_one_application(page, CANDIDATE, "test-worker")
    assert result.stop_reason == worker.StopReason.AUTH_REQUIRED
    assert app_repo.get_application(app["id"])["status"] == "FAILED_RETRYABLE"


# 4. Security challenge stops safely
def test_process_one_application_security_challenge_stops(fake_intervention_repo, page, monkeypatch):
    app = _make_queued_application()
    monkeypatch.setattr(worker, "open_job", lambda page, url: _nav_result(challenge=ChallengeType.CAPTCHA))

    result = worker.process_one_application(page, CANDIDATE, "test-worker")
    assert result.stop_reason == worker.StopReason.SECURITY_CHALLENGE
    assert app_repo.get_application(app["id"])["status"] == "FAILED_RETRYABLE"


# 5. Stale/already-applied job -> FAILED, not retryable
def test_process_one_application_already_applied(fake_intervention_repo, page, monkeypatch):
    app = _make_queued_application()
    monkeypatch.setattr(worker, "open_job", lambda page, url: _nav_result(already_applied=True))

    result = worker.process_one_application(page, CANDIDATE, "test-worker")
    assert result.stop_reason == worker.StopReason.ALREADY_APPLIED
    assert app_repo.get_application(app["id"])["status"] == "FAILED"


def test_process_one_application_stale_ineligible(fake_intervention_repo, page, monkeypatch):
    app = _make_queued_application()
    monkeypatch.setattr(worker, "open_job", lambda page, url: _nav_result(easy_apply_visible=False))

    result = worker.process_one_application(page, CANDIDATE, "test-worker")
    assert result.stop_reason == worker.StopReason.STALE_OR_INELIGIBLE
    assert app_repo.get_application(app["id"])["status"] == "FAILED"


# 6. Unsupported/unknown question with no trusted mapping -> NEEDS_INPUT, creates intervention
def test_process_one_application_needs_input_creates_intervention(fake_intervention_repo, page, monkeypatch):
    app = _make_queued_application()
    _patch_happy_path(monkeypatch, questions_extraction=_one_needs_input_question())

    result = worker.process_one_application(page, CANDIDATE, "test-worker")
    assert result.stop_reason == worker.StopReason.NEEDS_INPUT
    assert app_repo.get_application(app["id"])["status"] == "NEEDS_INPUT"

    interventions = [
        r for r in app_repo.get_supabase_client().tables["interventions"] if r["application_id"] == app["id"]
    ]
    assert len(interventions) == 1
    assert interventions[0]["options"]["question_id"] == "onsite-q-uuid"


# 7. Sequential: worker never claims a second application while one is mid-flight
def test_process_one_application_claims_at_most_one(fake_intervention_repo, page, monkeypatch):
    _make_queued_application(dice_job_id="DICE-6-A")
    _make_queued_application(dice_job_id="DICE-6-B")
    _patch_happy_path(monkeypatch)

    worker.process_one_application(page, CANDIDATE, "test-worker")

    client = app_repo.get_supabase_client()
    processing_or_further = [a for a in client.tables["applications"] if a["status"] != "QUEUED"]
    assert len(processing_or_further) == 1


# 8. AUTHORIZED_AUTONOMOUS path -- architected, exercised only offline
def test_process_one_application_authorized_autonomous_submits(fake_intervention_repo, page, monkeypatch):
    app = _make_queued_application()
    _patch_happy_path(monkeypatch)
    monkeypatch.setattr(
        worker,
        "submit_application",
        lambda page, url, app_id, job_id, preconditions, **kw: SubmissionResult(
            SubmissionStatus.VERIFIED_SUBMITTED, "ok", {"confirmation_text": "on its way"}, app_id, job_id, url, url + "/success"
        ),
    )

    result = worker.process_one_application(page, CANDIDATE, "test-worker", submission_policy=worker.SubmissionPolicy.AUTHORIZED_AUTONOMOUS)
    assert result.stop_reason == worker.StopReason.VERIFIED_SUBMITTED
    assert app_repo.get_application(app["id"])["status"] == "SUBMITTED"


def test_process_one_application_authorized_autonomous_uncertain_never_marks_submitted(fake_intervention_repo, page, monkeypatch):
    app = _make_queued_application()
    _patch_happy_path(monkeypatch)
    monkeypatch.setattr(
        worker,
        "submit_application",
        lambda page, url, app_id, job_id, preconditions, **kw: SubmissionResult(
            SubmissionStatus.VERIFICATION_UNCERTAIN, "no evidence", {}, app_id, job_id, url, url
        ),
    )

    result = worker.process_one_application(page, CANDIDATE, "test-worker", submission_policy=worker.SubmissionPolicy.AUTHORIZED_AUTONOMOUS)
    assert result.stop_reason == worker.StopReason.VERIFICATION_UNCERTAIN
    assert app_repo.get_application(app["id"])["status"] != "SUBMITTED"


# ── run_worker: sequencing + circuit breaker ──────────────────────────────


def test_run_worker_stops_when_nothing_queued(fake_intervention_repo, page, monkeypatch):
    monkeypatch.setattr(
        worker, "process_one_application", lambda *a, **k: worker.ApplicationRunResult(None, None, worker.StopReason.NOTHING_QUEUED, "empty")
    )
    summary = worker.run_worker(page, CANDIDATE, "test-worker", max_applications=5)
    assert len(summary.processed) == 1
    assert summary.halted is False


def test_run_worker_trips_circuit_breaker_on_repeated_auth_required(fake_intervention_repo, page, monkeypatch):
    monkeypatch.setattr(
        worker,
        "process_one_application",
        lambda *a, **k: worker.ApplicationRunResult("app-x", "job-x", worker.StopReason.AUTH_REQUIRED, "not authenticated"),
    )
    summary = worker.run_worker(page, CANDIDATE, "test-worker", max_applications=10, circuit_breaker_threshold=3)
    assert summary.halted is True
    assert len(summary.processed) == 3  # stopped after exactly the threshold, not all 10


def test_run_worker_resets_circuit_breaker_after_a_non_session_stop(fake_intervention_repo, page, monkeypatch):
    calls = {"n": 0}

    def fake_process(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            return worker.ApplicationRunResult("app-mid", "job-mid", worker.StopReason.NEEDS_INPUT, "blocked")
        return worker.ApplicationRunResult("app-x", "job-x", worker.StopReason.AUTH_REQUIRED, "not authenticated")

    monkeypatch.setattr(worker, "process_one_application", fake_process)
    summary = worker.run_worker(page, CANDIDATE, "test-worker", max_applications=6, circuit_breaker_threshold=3)
    # 2 AUTH_REQUIRED, then NEEDS_INPUT resets the counter, then 3 more AUTH_REQUIRED trips it
    assert summary.halted is True
    assert len(summary.processed) == 5


def test_run_worker_never_processes_more_than_max_applications(fake_intervention_repo, page, monkeypatch):
    monkeypatch.setattr(
        worker,
        "process_one_application",
        lambda *a, **k: worker.ApplicationRunResult("app-x", "job-x", worker.StopReason.NEEDS_INPUT, "blocked"),
    )
    summary = worker.run_worker(page, CANDIDATE, "test-worker", max_applications=2)
    assert len(summary.processed) == 2
    assert summary.halted is False


# ── resume_needs_input_application ────────────────────────────────────────


def test_resume_needs_input_application_rejects_wrong_status(fake_intervention_repo, page, monkeypatch):
    app = _make_queued_application()
    result = worker.resume_needs_input_application(page, app["id"], "test-worker")
    assert result.stop_reason == worker.StopReason.NOT_YET_RESUMABLE


def test_resume_needs_input_application_blocks_when_not_all_resolved(fake_intervention_repo, page, monkeypatch):
    app = _make_queued_application()
    app_repo.update_application_status(app["id"], "PROCESSING", worker_id="test-worker")
    app_repo.update_application_status(app["id"], "NEEDS_INPUT")
    import db.intervention_repository as iv_repo

    iv_repo.create_or_get_question_intervention(
        application_id=app["id"], question_id="onsite-q-uuid", question_prompt="onsite?",
        field_type="RADIO", reason="no trusted mapping", choices=["Yes", "No"], sensitive=False,
    )

    result = worker.resume_needs_input_application(page, app["id"], "test-worker")
    assert result.stop_reason == worker.StopReason.NEEDS_INPUT


def test_resume_needs_input_application_fills_resolved_answer_and_reaches_review(fake_intervention_repo, page, monkeypatch):
    app = _make_queued_application()
    app_repo.update_application_status(app["id"], "PROCESSING", worker_id="test-worker")
    app_repo.update_application_status(app["id"], "NEEDS_INPUT")
    import db.intervention_repository as iv_repo

    intervention = iv_repo.create_or_get_question_intervention(
        application_id=app["id"], question_id="onsite-q-uuid", question_prompt="onsite?",
        field_type="RADIO", reason="no trusted mapping", choices=["Yes", "No"], sensitive=False,
    )
    iv_repo.resolve_question_intervention(intervention["id"], "Yes", source="human")

    _patch_happy_path(monkeypatch, questions_extraction=_one_needs_input_question())
    filled = {}
    monkeypatch.setattr(worker, "fill_answer", lambda page, question, answer: filled.__setitem__(question.question_id, answer))

    result = worker.resume_needs_input_application(page, app["id"], "test-worker")
    assert filled == {"onsite-q-uuid": "Yes"}
    assert result.stop_reason == worker.StopReason.AWAITING_SUBMIT_CONFIRMATION
