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

import uuid

import pytest
from playwright.sync_api import sync_playwright

import db.application_repository as app_repo
import dice_browser.worker as worker
from attention.channels import bind_channel
from db import dice_auth_health_repository
from db.supabase_client import get_supabase_client
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

    # Real bug found via live-Supabase bounded-run testing (2026-08-22):
    # an application awaiting confirmation must not stay PROCESSING
    # forever -- claim_next_queued_application()'s own "no other
    # PROCESSING/SUBMITTING application for this candidate" gate would
    # then permanently block every subsequent claim for that candidate,
    # so a multi-job run under REQUIRE_CONFIRMATION could only ever
    # process its first job. NEEDS_INPUT is the existing, already-modeled
    # "doesn't block sibling claims" status (Phase 1's own claim RPC
    # comment: "does NOT block on APPLICATION_LEVEL NEEDS_INPUT") -- reused
    # here rather than adding a new status value/migration.
    assert app_repo.get_application(app["id"])["status"] == "NEEDS_INPUT"


def test_process_one_application_awaiting_confirmation_does_not_block_next_claim_for_same_candidate(
    fake_intervention_repo, page, monkeypatch
):
    app_a = _make_queued_application(dice_job_id="DICE-6-CONFIRM-A")
    app_b = _make_queued_application(dice_job_id="DICE-6-CONFIRM-B")
    _patch_happy_path(monkeypatch)

    first = worker.process_one_application(page, CANDIDATE, "test-worker")
    assert first.application_id == app_a["id"]
    assert first.stop_reason == worker.StopReason.AWAITING_SUBMIT_CONFIRMATION

    second = worker.process_one_application(page, CANDIDATE, "test-worker")
    assert second.application_id == app_b["id"]
    assert second.stop_reason == worker.StopReason.AWAITING_SUBMIT_CONFIRMATION


# 3. AUTH_REQUIRED on live re-check stops safely, marks FAILED_RETRYABLE
def test_process_one_application_auth_required_stops(fake_intervention_repo, page, monkeypatch):
    app = _make_queued_application()
    monkeypatch.setattr(worker, "open_job", lambda page, url: _nav_result(authenticated=False))

    result = worker.process_one_application(page, CANDIDATE, "test-worker")
    assert result.stop_reason == worker.StopReason.AUTH_REQUIRED
    assert app_repo.get_application(app["id"])["status"] == "FAILED_RETRYABLE"


# Real gap, live-found 2026-08-24/25: every failure path went through
# _fail() but never told the candidate -- a real user who tapped Apply
# would see "Checking the application..." and then silence forever on
# anything but a genuine SUBMITTED. _fail() now best-effort notifies
# through whatever channel is actually bound.
def test_process_one_application_failure_notifies_bound_candidate(fake_intervention_repo, page, monkeypatch, request):
    import requests

    app = _make_queued_application()
    channel = bind_channel(CANDIDATE, "TELEGRAM", "12345", verified=True)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    request.addfinalizer(lambda: get_supabase_client().table("candidate_attention_channels").delete().eq("id", channel["id"]).execute())

    sent = []

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": True, "result": {"message_id": 1}}

    def fake_post(url, json=None, **kw):
        sent.append((url, json))
        return _FakeResponse()

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(worker, "open_job", lambda page, url: _nav_result(authenticated=False))

    worker.process_one_application(page, CANDIDATE, "test-worker")

    failure_sends = [call for call in sent if "sendMessage" in call[0]]
    assert len(failure_sends) == 1
    assert failure_sends[0][1]["chat_id"] == "12345"
    assert "AUTH_REQUIRED" in failure_sends[0][1]["text"] or "auth" in failure_sends[0][1]["text"].lower()


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


# 6b. A prior answer this candidate already gave for this exact
# question_id (on a different application) is reused directly -- no
# intervention created, no NEEDS_INPUT stop.
def test_process_one_application_reuses_prior_answer_no_intervention(fake_intervention_repo, page, monkeypatch):
    app = _make_queued_application()
    _patch_happy_path(monkeypatch, questions_extraction=_one_needs_input_question())
    fill_calls = []
    monkeypatch.setattr(worker, "fill_answer", lambda page, question, answer: fill_calls.append((question.question_id, answer)))
    monkeypatch.setattr(worker, "find_reusable_answer", lambda candidate_id, question_id: "Yes" if question_id == "onsite-q-uuid" else None)

    result = worker.process_one_application(page, CANDIDATE, "test-worker")

    assert fill_calls == [("onsite-q-uuid", "Yes")]
    interventions = [
        r for r in app_repo.get_supabase_client().tables["interventions"] if r["application_id"] == app["id"]
    ]
    assert interventions == []
    assert result.stop_reason != worker.StopReason.NEEDS_INPUT


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
    application = app_repo.get_application(app["id"])
    assert application["status"] != "SUBMITTED"
    # Regression, live-found 2026-08-22: a non-VERIFIED_SUBMITTED result
    # used to leave the application stuck at SUBMITTING forever (no valid
    # transition back to QUEUED from there) -- must land on a real
    # terminal/retryable status instead.
    assert application["status"] == "FAILED_RETRYABLE"


# 8b. Live-found 2026-08-24: a genuinely successful submit whose
# confirmation banner just wasn't detected in time must self-resolve via
# a live already_applied re-check, never require a human to manually
# re-verify -- see worker._resolve_uncertain_via_already_applied.
def test_process_one_application_uncertain_self_resolves_via_already_applied_recheck(fake_intervention_repo, page, monkeypatch):
    app = _make_queued_application()
    _patch_happy_path(monkeypatch)
    # First open_job call (pre-Easy-Apply) reports not yet applied; the
    # fallback recheck (post-submit) reports applied -- exactly what a
    # real successful submit looks like from Dice's own perspective.
    open_job_calls = {"count": 0}

    def fake_open_job(page, url):
        open_job_calls["count"] += 1
        return _nav_result(already_applied=open_job_calls["count"] > 1)

    monkeypatch.setattr(worker, "open_job", fake_open_job)
    monkeypatch.setattr(
        worker,
        "submit_application",
        lambda page, url, app_id, job_id, preconditions, **kw: SubmissionResult(
            SubmissionStatus.VERIFICATION_UNCERTAIN,
            "URL left the wizard but no explicit confirmation text was found",
            {},
            app_id,
            job_id,
            url,
            url,
        ),
    )

    result = worker.process_one_application(page, CANDIDATE, "test-worker", submission_policy=worker.SubmissionPolicy.AUTHORIZED_AUTONOMOUS)

    assert result.stop_reason == worker.StopReason.VERIFIED_SUBMITTED
    application = app_repo.get_application(app["id"])
    assert application["status"] == "SUBMITTED"
    assert application["verification_evidence"]["fallback_check"] == "already_applied_recheck"
    assert open_job_calls["count"] == 2


# 8c. The fallback must never paper over genuine ambiguity -- if the
# recheck itself can't confirm applied (still False, or errors), the
# original uncertain result stands unchanged.
def test_process_one_application_uncertain_stays_uncertain_when_recheck_says_not_applied(fake_intervention_repo, page, monkeypatch):
    app = _make_queued_application()
    _patch_happy_path(monkeypatch)  # open_job always reports already_applied=False
    monkeypatch.setattr(
        worker,
        "submit_application",
        lambda page, url, app_id, job_id, preconditions, **kw: SubmissionResult(
            SubmissionStatus.VERIFICATION_UNCERTAIN, "no evidence", {}, app_id, job_id, url, url
        ),
    )

    result = worker.process_one_application(page, CANDIDATE, "test-worker", submission_policy=worker.SubmissionPolicy.AUTHORIZED_AUTONOMOUS)

    assert result.stop_reason == worker.StopReason.VERIFICATION_UNCERTAIN
    assert app_repo.get_application(app["id"])["status"] == "FAILED_RETRYABLE"


def test_process_one_application_authorized_autonomous_submit_failed_marks_failed_not_stuck_submitting(fake_intervention_repo, page, monkeypatch):
    app = _make_queued_application()
    _patch_happy_path(monkeypatch)
    monkeypatch.setattr(
        worker,
        "submit_application",
        lambda page, url, app_id, job_id, preconditions, **kw: SubmissionResult(
            SubmissionStatus.SUBMIT_FAILED, "current page URL does not match the expected application/job", {}, app_id, job_id, url, url
        ),
    )

    result = worker.process_one_application(page, CANDIDATE, "test-worker", submission_policy=worker.SubmissionPolicy.AUTHORIZED_AUTONOMOUS)
    assert result.stop_reason == worker.StopReason.SUBMIT_FAILED
    application = app_repo.get_application(app["id"])
    assert application["status"] == "FAILED"
    assert application["status"] != "SUBMITTING"


# Regression, live-found 2026-08-22: the first genuine live exercise of
# AUTHORIZED_AUTONOMOUS (real CDP browser, real Easy Apply, real Review
# screen) failed submit_application's own pre-submit URL gate every time
# -- SUBMIT_FAILED, "current page URL does not match the expected
# application/job", Submit never even clicked. Root cause: worker.py
# passed canonical_url (the job-DETAIL page, e.g. .../job-detail/{id}) as
# submit_application's expected_job_url_fragment, but by Review time the
# live page is on the wizard URL (.../job-applications/{id}/wizard) --
# same job id fragment, different path prefix, so the substring check
# always failed. submit_application's own test suite
# (test_dice_browser_submission.py, JOB_FRAGMENT = "TESTJOB123" against
# a .../job-applications/TESTJOB123/wizard page) already modeled the
# CORRECT contract -- expected_job_url_fragment is just the job id
# fragment, not the full detail-page URL -- so this was purely a
# wrong-argument bug in the one caller, invisible to every existing test
# because they all replace submit_application with a lambda that never
# inspects its own `url` argument.
def test_process_one_application_authorized_autonomous_passes_job_id_fragment_not_detail_url(fake_intervention_repo, page, monkeypatch):
    app = _make_queued_application()  # canonical_url = "https://dice.com/job-detail/DICE-6-1"
    _patch_happy_path(monkeypatch)
    captured = {}

    def _fake_submit(page, url, app_id, job_id, preconditions, **kw):
        captured["url"] = url
        return SubmissionResult(SubmissionStatus.VERIFIED_SUBMITTED, "ok", {"confirmation_text": "on its way"}, app_id, job_id, url, url + "/success")

    monkeypatch.setattr(worker, "submit_application", _fake_submit)

    worker.process_one_application(page, CANDIDATE, "test-worker", submission_policy=worker.SubmissionPolicy.AUTHORIZED_AUTONOMOUS)

    assert captured["url"] == "DICE-6-1"  # the fragment that actually appears in the wizard URL, not the job-detail page


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


# ── Phase 7.6: DICEPILOT_PROOF_STOP_AFTER_EASY_APPLY_OPEN ────────────────
# Env-gated, default-off guard for live-proving the Telegram->Browserless
# bridge up to the real Easy Apply wizard without ever risking a real
# resume upload/question fill/submit. Deliberately reuses PROCESSING
# (the status the atomic claim already set) rather than inventing a new
# terminal status just for this proof.


def test_process_one_application_proof_stop_after_easy_apply_open(fake_intervention_repo, page, monkeypatch):
    app = _make_queued_application()
    _patch_happy_path(monkeypatch)
    monkeypatch.setenv("DICEPILOT_PROOF_STOP_AFTER_EASY_APPLY_OPEN", "true")

    def _must_not_be_called(*a, **kw):
        raise AssertionError("resume/question/submit flow must never run when the proof-stop guard is active")

    monkeypatch.setattr(worker, "detect_existing_resume", _must_not_be_called)
    monkeypatch.setattr(worker, "click_next", _must_not_be_called)
    monkeypatch.setattr(worker, "extract_questions", _must_not_be_called)

    result = worker.process_one_application(page, CANDIDATE, "test-worker")

    assert result.application_id == app["id"]
    assert result.stop_reason == worker.StopReason.PROOF_STOP_EASY_APPLY_OPENED
    assert app_repo.get_application(app["id"])["status"] == "PROCESSING"

    events = [e for e in app_repo.get_supabase_client().tables["application_events"] if e["application_id"] == app["id"]]
    assert any(e["event_type"] == "easy_apply_opened" for e in events)


def test_process_one_application_proof_stop_disabled_by_default(fake_intervention_repo, page, monkeypatch):
    app = _make_queued_application()
    _patch_happy_path(monkeypatch)
    monkeypatch.delenv("DICEPILOT_PROOF_STOP_AFTER_EASY_APPLY_OPEN", raising=False)

    result = worker.process_one_application(page, CANDIDATE, "test-worker")

    assert result.stop_reason != worker.StopReason.PROOF_STOP_EASY_APPLY_OPENED


# ── Phase 8B: real Dice auth verification durably updates dice_auth_health ──
# Isolated per-test candidate UUIDs (never the shared file-level CANDIDATE)
# so these never interact with production auth-health state.


@pytest.fixture(autouse=True)
def _cleanup_auth_health():
    """Autouse for every test in this file, not just the Phase 8B ones
    below -- process_one_application/resume_needs_input_application now
    write real dice_auth_health rows as a side effect of ANY successful
    open_job() call, including in every pre-existing test that uses the
    shared file-level CANDIDATE constant. Always sweeping CANDIDATE too
    (not just whatever a specific test appends) is what actually keeps
    this file pollution-free after Phase B's wiring landed."""
    created: list[str] = [CANDIDATE]
    yield created
    client = get_supabase_client()
    for cid in set(created):
        client.table("dice_auth_health").delete().eq("candidate_id", cid).execute()


def _make_queued_application_for(candidate_id, dice_job_id):
    # www.dice.com specifically (not bare dice.com) -- navigator.
    # validate_canonical_url requires the exact allowed host, and this
    # helper (unlike _make_queued_application above) is used by tests
    # that exercise the REAL open_job(), not a monkeypatched stand-in.
    job = app_repo.upsert_dice_job(
        {"dice_job_id": dice_job_id, "canonical_url": f"https://www.dice.com/job-detail/{dice_job_id}", "title": "Worker Test Role"}
    )
    return app_repo.enqueue_application(candidate_id, job["id"])


# 1. final ACTIVE auth result -> mark_healthy
def test_process_one_application_marks_auth_healthy_on_active(fake_intervention_repo, page, monkeypatch, _cleanup_auth_health):
    candidate_id = str(uuid.uuid4())
    _cleanup_auth_health.append(candidate_id)
    app = _make_queued_application_for(candidate_id, f"DICE-8B-{uuid.uuid4()}")
    _patch_happy_path(monkeypatch)  # open_job -> authenticated=True

    worker.process_one_application(page, candidate_id, "test-worker")

    health = dice_auth_health_repository.get_auth_health(candidate_id)
    assert health["is_healthy"] is True


# 3. final AUTH_REQUIRED -> mark_invalid
def test_process_one_application_marks_auth_invalid_on_auth_required(fake_intervention_repo, page, monkeypatch, _cleanup_auth_health):
    candidate_id = str(uuid.uuid4())
    _cleanup_auth_health.append(candidate_id)
    app = _make_queued_application_for(candidate_id, f"DICE-8B-{uuid.uuid4()}")
    monkeypatch.setattr(worker, "open_job", lambda page, url: _nav_result(authenticated=False))

    worker.process_one_application(page, candidate_id, "test-worker")

    health = dice_auth_health_repository.get_auth_health(candidate_id)
    assert health["is_healthy"] is False
    assert "AUTH_REQUIRED" in health["invalidated_reason"] or "auth" in health["invalidated_reason"].lower()


# 4. SECURITY_CHALLENGE -> never healthy, distinct invalid reason
def test_process_one_application_marks_auth_invalid_on_security_challenge(fake_intervention_repo, page, monkeypatch, _cleanup_auth_health):
    from dice_browser.models import ChallengeType

    candidate_id = str(uuid.uuid4())
    _cleanup_auth_health.append(candidate_id)
    app = _make_queued_application_for(candidate_id, f"DICE-8B-{uuid.uuid4()}")
    monkeypatch.setattr(worker, "open_job", lambda page, url: _nav_result(challenge=ChallengeType.CAPTCHA))

    worker.process_one_application(page, candidate_id, "test-worker")

    health = dice_auth_health_repository.get_auth_health(candidate_id)
    assert health["is_healthy"] is False
    assert "SECURITY_CHALLENGE" in health["invalidated_reason"]


# 2. the transient first-load hydration race, resolved by navigator.open_job's
# own retry, must never poison auth health -- worker._record_auth_health only
# ever sees the FINAL bounded NavigationResult, proven end to end with the
# REAL open_job (not monkeypatched away) against a stateful fake page.
def test_hydration_race_recovery_ends_up_healthy_never_invalid(fake_intervention_repo, page, monkeypatch, _cleanup_auth_health):
    from dice_browser.navigator import open_job as real_open_job

    candidate_id = str(uuid.uuid4())
    _cleanup_auth_health.append(candidate_id)
    app = _make_queued_application_for(candidate_id, f"DICE-8B-{uuid.uuid4()}")
    _patch_happy_path(monkeypatch)

    login_html = '<html><body><a href="/dashboard/login">Login</a></body></html>'
    authenticated_html = '<html><body><nav aria-label="Account"></nav><apply-button-wc></apply-button-wc></body></html>'
    state = {"loaded": False}

    def fake_goto(target_url, **kwargs):
        page.set_content(login_html)
        return None

    def fake_reload(**kwargs):
        state["loaded"] = True
        page.set_content(authenticated_html)
        return None

    monkeypatch.setattr(worker, "open_job", real_open_job)
    monkeypatch.setattr(page, "goto", fake_goto)
    monkeypatch.setattr(page, "reload", fake_reload)

    worker.process_one_application(page, candidate_id, "test-worker")

    assert state["loaded"] is True  # the retry actually ran
    health = dice_auth_health_repository.get_auth_health(candidate_id)
    assert health["is_healthy"] is True  # never left invalid from the transient first check


# 5. a later mark_invalid always overrides a previous healthy TTL window
# (this is readiness.check_dice_auth_ready's own contract -- already proven
# directly in tests/test_readiness.py; re-asserted here at the repository
# level for Phase B's own test list).
def test_mark_invalid_after_healthy_overrides_regardless_of_ttl(_cleanup_auth_health):
    candidate_id = str(uuid.uuid4())
    _cleanup_auth_health.append(candidate_id)
    dice_auth_health_repository.mark_healthy(candidate_id)
    dice_auth_health_repository.mark_invalid(candidate_id, "AUTH_REQUIRED on live re-check")

    health = dice_auth_health_repository.get_auth_health(candidate_id)
    assert health["is_healthy"] is False


# 6. worker restart never invents healthy auth without a real verification --
# nothing in this file marks a candidate healthy except a real open_job()
# call inside process_one_application/resume_needs_input_application; a
# freshly-started worker with no prior verification simply has no row yet.
def test_no_auth_health_row_exists_without_a_real_verification(_cleanup_auth_health):
    candidate_id = str(uuid.uuid4())
    _cleanup_auth_health.append(candidate_id)
    assert dice_auth_health_repository.get_auth_health(candidate_id) is None
