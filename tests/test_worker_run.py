"""Phase 6.2: dice_browser.worker's bounded-run mode -- the worker only
ever processes applications belonging to one run_registry run (enforced
by the real claim_next_queued_application_for_run() RPC, migration
20260822010000_application_runs.sql), sequentially, and never queries the
DB pool for "whatever else is QUEUED". This is the test coverage for the
critical requirement: "select 5 jobs" must never become "process every
queued job".

Live Supabase for run/application/claim state (matching this project's
own established rule: atomic-claim behavior can't be meaningfully faked
in-process -- see tests/test_worker_integration.py), browser layer mocked
the same way as tests/test_worker.py so this never touches live Dice.
"""
from __future__ import annotations

import uuid

import pytest
from playwright.sync_api import sync_playwright

import db.application_repository as app_repo
import dice_browser.worker as worker
import run_registry
from dice.models import CandidateFetchResult, CandidateFetchStatus, CandidateProfile
from dice_browser.models import BrowserState, EasyApplyOpenResult, NavigationResult, QuestionExtractionResult, QuestionExtractionStatus

CANDIDATE = "66666666-6666-6666-6666-666666666666"


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


def _make_queued_application(dice_job_id):
    job = app_repo.upsert_dice_job(
        {"dice_job_id": dice_job_id, "canonical_url": f"https://dice.com/job-detail/{dice_job_id}", "title": "Worker Run Test Role"}
    )
    application = app_repo.enqueue_application(CANDIDATE, job["id"])
    return job, application


def _cleanup(*job_ids: str, run_ids: list[str] = ()):
    sc = app_repo.get_supabase_client()
    for job_id in job_ids:
        apps = sc.table("applications").select("id").eq("dice_job_id", job_id).execute().data
        for a in apps:
            sc.table("interventions").delete().eq("application_id", a["id"]).execute()
            sc.table("application_events").delete().eq("application_id", a["id"]).execute()
            sc.table("applications").delete().eq("id", a["id"]).execute()
        sc.table("dice_jobs").delete().eq("id", job_id).execute()
    for run_id in run_ids:
        sc.table("application_runs").delete().eq("id", run_id).execute()


def _nav_result(authenticated=True, already_applied=False, easy_apply_visible=True, challenge=None):
    return NavigationResult(
        canonical_url="https://dice.com/job-detail/x",
        page_title="Worker Run Test Role",
        browser_state=BrowserState.ACTIVE if authenticated else BrowserState.AUTH_REQUIRED,
        authenticated=authenticated,
        already_applied=already_applied,
        easy_apply_visible=easy_apply_visible,
        challenge_type=challenge,
        evidence="test",
    )


def _no_questions_extraction():
    return QuestionExtractionResult(status=QuestionExtractionStatus.NO_QUESTIONS_PRESENT, questions=())


def _patch_happy_path(monkeypatch, total_steps=2):
    state = {"step": 1}
    monkeypatch.setattr(worker, "open_job", lambda page, url: _nav_result())
    monkeypatch.setattr(worker, "open_easy_apply", lambda page, nav: EasyApplyOpenResult(True, "url", "title", "OPENED"))
    monkeypatch.setattr(worker, "detect_existing_resume", lambda page: True)
    monkeypatch.setattr(worker, "is_review_screen", lambda page: state["step"] >= total_steps)
    monkeypatch.setattr(worker, "extract_questions", lambda page: _no_questions_extraction())

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
                candidate_id=candidate_id, name=None, email=None, phone=None, location=None, visa_type=None,
                work_authorized=None, requires_sponsorship=None, willing_to_relocate=None, experience_years=None,
                desired_start_date=None, resume_url=None, linkedin_url=None, github_url=None,
            ),
            None,
        ),
    )


# 1. Processes exactly the given ids
def test_run_worker_for_run_processes_exact_ids(live_client, page, monkeypatch):
    job_a, app_a = _make_queued_application("RUN-A")
    job_b, app_b = _make_queued_application("RUN-B")
    _patch_happy_path(monkeypatch)
    run = run_registry.create_run([app_a["id"], app_b["id"]], candidate_id=CANDIDATE)
    try:
        summary = worker.run_worker_for_run(page, run["id"], "test-worker")

        # Ends with a trailing NOTHING_QUEUED once the run is exhausted --
        # same convention run_worker() already uses for its own loop.
        job_results = [r for r in summary.processed if r.application_id is not None]
        assert {r.application_id for r in job_results} == {app_a["id"], app_b["id"]}
        assert all(r.stop_reason == worker.StopReason.AWAITING_SUBMIT_CONFIRMATION for r in job_results)
        assert summary.processed[-1].stop_reason == worker.StopReason.NOTHING_QUEUED
    finally:
        _cleanup(job_a["id"], job_b["id"], run_ids=[run["id"]])


# 2. THE critical one: an unrelated QUEUED application outside the run is never touched
def test_run_worker_for_run_never_touches_unrelated_queued_application(live_client, page, monkeypatch):
    job_in, app_in = _make_queued_application("RUN-IN")
    job_out, app_out = _make_queued_application("RUN-OUTSIDE")
    _patch_happy_path(monkeypatch)
    run = run_registry.create_run([app_in["id"]], candidate_id=CANDIDATE)
    try:
        worker.run_worker_for_run(page, run["id"], "test-worker")

        assert app_repo.get_application(app_out["id"])["status"] == "QUEUED"
        assert app_repo.get_application(app_in["id"])["status"] != "QUEUED"
    finally:
        _cleanup(job_in["id"], job_out["id"], run_ids=[run["id"]])


# 3. Run status transitions QUEUED -> RUNNING -> COMPLETE
def test_run_worker_for_run_updates_run_status_to_complete(live_client, page, monkeypatch):
    job, app = _make_queued_application("RUN-STATUS")
    _patch_happy_path(monkeypatch)
    run = run_registry.create_run([app["id"]], candidate_id=CANDIDATE)
    try:
        worker.run_worker_for_run(page, run["id"], "test-worker")
        assert run_registry.get_run(run["id"])["status"] == "COMPLETE"
    finally:
        _cleanup(job["id"], run_ids=[run["id"]])


# 4. Stop Run (checked before claiming the next application) prevents the next job from starting
def test_run_worker_for_run_stop_prevents_next_job(live_client, page, monkeypatch):
    job_a, app_a = _make_queued_application("RUN-STOP-A")
    job_b, app_b = _make_queued_application("RUN-STOP-B")
    _patch_happy_path(monkeypatch)
    run = run_registry.create_run([app_a["id"], app_b["id"]], candidate_id=CANDIDATE)
    try:
        real_process = worker.process_one_application

        def stop_after_first(*a, **k):
            result = real_process(*a, **k)
            run_registry.request_stop(run["id"])
            return result

        monkeypatch.setattr(worker, "process_one_application", stop_after_first)

        summary = worker.run_worker_for_run(page, run["id"], "test-worker")

        assert len(summary.processed) == 1
        assert summary.halted is True
        remaining = app_b if summary.processed[0].application_id == app_a["id"] else app_a
        assert app_repo.get_application(remaining["id"])["status"] == "QUEUED"  # never claimed
        assert run_registry.get_run(run["id"])["status"] == "STOPPED"
    finally:
        _cleanup(job_a["id"], job_b["id"], run_ids=[run["id"]])


# 5. A session-level stop halts the whole run immediately -- not after 3 like run_worker()'s circuit breaker
def test_run_worker_for_run_halts_immediately_on_session_level_stop(live_client, page, monkeypatch):
    job_a, app_a = _make_queued_application("RUN-AUTH-A")
    job_b, app_b = _make_queued_application("RUN-AUTH-B")
    monkeypatch.setattr(worker, "open_job", lambda page, url: _nav_result(authenticated=False))
    run = run_registry.create_run([app_a["id"], app_b["id"]], candidate_id=CANDIDATE)
    try:
        summary = worker.run_worker_for_run(page, run["id"], "test-worker")

        assert len(summary.processed) == 1
        assert summary.halted is True
        remaining = app_b if summary.processed[0].application_id == app_a["id"] else app_a
        assert app_repo.get_application(remaining["id"])["status"] == "QUEUED"
    finally:
        _cleanup(job_a["id"], job_b["id"], run_ids=[run["id"]])


# 6. NEEDS_INPUT on one job does not block the next independent selected job
def test_run_worker_for_run_needs_input_does_not_block_next_job(live_client, page, monkeypatch):
    from dice_browser.models import FieldType, QuestionField, QuestionStatus, RequiredState

    job_a, app_a = _make_queued_application("RUN-NI-A")
    job_b, app_b = _make_queued_application("RUN-NI-B")
    _patch_happy_path(monkeypatch, total_steps=3)
    question = QuestionField(
        question_id="q-1", prompt="Expected salary?", field_type=FieldType.TEXTAREA,
        required_state=RequiredState.UNKNOWN, options=None, current_value=None, helper=None, status=QuestionStatus.NEEDS_INPUT,
    )
    monkeypatch.setattr(worker, "extract_questions", lambda page: QuestionExtractionResult(QuestionExtractionStatus.QUESTIONS_PRESENT, (question,)))
    run = run_registry.create_run([app_a["id"], app_b["id"]], candidate_id=CANDIDATE)
    try:
        summary = worker.run_worker_for_run(page, run["id"], "test-worker")

        needs_input_results = [r for r in summary.processed if r.stop_reason == worker.StopReason.NEEDS_INPUT]
        assert len(needs_input_results) == 1
        # both applications were still claimed and processed despite one pausing
        job_results = [r for r in summary.processed if r.application_id is not None]
        assert len(job_results) == 2
        assert app_repo.get_application(app_a["id"])["status"] != "QUEUED"
        assert app_repo.get_application(app_b["id"])["status"] != "QUEUED"
    finally:
        _cleanup(job_a["id"], job_b["id"], run_ids=[run["id"]])


# 7. An application that's no longer QUEUED by the time its turn comes (a run of exactly one,
#    already terminal) is never re-processed/re-submitted -- the RPC simply finds nothing to claim.
def test_run_worker_for_run_finishes_cleanly_when_nothing_left_to_claim(live_client, page, monkeypatch):
    job, app = _make_queued_application("RUN-SKIP-A")
    _patch_happy_path(monkeypatch)
    run = run_registry.create_run([app["id"]], candidate_id=CANDIDATE)
    app_repo.update_application_status(app["id"], "PROCESSING")
    app_repo.update_application_status(app["id"], "FAILED", error_code="TEST", error_message="already terminal")
    try:
        summary = worker.run_worker_for_run(page, run["id"], "test-worker")

        assert len(summary.processed) == 1
        assert summary.processed[0].stop_reason == worker.StopReason.NOTHING_QUEUED
        assert app_repo.get_application(app["id"])["status"] == "FAILED"  # untouched, not re-processed
    finally:
        _cleanup(job["id"], run_ids=[run["id"]])


# 8. Phase 7.6: the Telegram Apply bridge -- a QUEUED application/run
# created via attention.service.handle_apply() (exactly what a real
# Telegram Apply tap produces) is claimed and processed by the real
# worker exactly like any other QUEUED application -- the worker has no
# special case for how QUEUED status was reached. Also exercises the
# live-proof guard (DICEPILOT_PROOF_STOP_AFTER_EASY_APPLY_OPEN) inside
# the real run_worker_for_run path, not just process_one_application
# directly.
def test_run_worker_for_run_processes_telegram_apply_created_application(live_client, page, monkeypatch):
    from attention.service import handle_apply
    from db.application_repository import create_job_offer

    job = app_repo.upsert_dice_job(
        {"dice_job_id": "RUN-TELEGRAM-BRIDGE", "canonical_url": "https://dice.com/job-detail/RUN-TELEGRAM-BRIDGE", "title": "Worker Test Role"}
    )
    offer = create_job_offer(CANDIDATE, job["id"])
    handle_apply(offer["id"])  # exactly what attention.service.handle_event does on a real Apply tap
    application = app_repo.get_application(offer["id"])
    run_id = application["run_id"]
    assert run_id is not None
    assert application["status"] == "QUEUED"

    _patch_happy_path(monkeypatch)
    monkeypatch.setenv("DICEPILOT_PROOF_STOP_AFTER_EASY_APPLY_OPEN", "true")
    try:
        summary = worker.run_worker_for_run(page, run_id, "test-worker")

        job_results = [r for r in summary.processed if r.application_id is not None]
        assert len(job_results) == 1
        assert job_results[0].application_id == offer["id"]
        assert job_results[0].stop_reason == worker.StopReason.PROOF_STOP_EASY_APPLY_OPENED
        assert app_repo.get_application(offer["id"])["status"] == "PROCESSING"
    finally:
        _cleanup(job["id"], run_ids=[run_id])
