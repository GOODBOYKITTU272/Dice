"""Phase 6.2: dice_browser.worker's bounded-run mode -- the worker only
ever processes the exact application_ids listed in a run_registry run,
sequentially, and never queries the DB pool for "whatever else is
QUEUED". This is the test coverage for the critical requirement:
"select 5 jobs" must never become "process every queued job".
"""
from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright

import db.application_repository as app_repo
import dice_browser.worker as worker
import run_registry
from dice.models import CandidateFetchResult, CandidateFetchStatus, CandidateProfile
from dice_browser.models import BrowserState, ChallengeType, EasyApplyOpenResult, NavigationResult, QuestionExtractionResult, QuestionExtractionStatus

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


@pytest.fixture(autouse=True)
def _isolated_runs_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(run_registry, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(worker, "run_registry", run_registry)


def _make_queued_application(dice_job_id):
    job = app_repo.upsert_dice_job(
        {"dice_job_id": dice_job_id, "canonical_url": f"https://dice.com/job-detail/{dice_job_id}", "title": "Worker Run Test Role"}
    )
    return app_repo.enqueue_application(CANDIDATE, job["id"])


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


# 1. Processes exactly the given ids, in order
def test_run_worker_for_run_processes_exact_ids_in_order(fake_intervention_repo, page, monkeypatch):
    app_a = _make_queued_application("RUN-A")
    app_b = _make_queued_application("RUN-B")
    _patch_happy_path(monkeypatch)
    run = run_registry.create_run([app_a["id"], app_b["id"]], candidate_id=CANDIDATE)

    summary = worker.run_worker_for_run(page, run["id"], "test-worker")

    assert [r.application_id for r in summary.processed] == [app_a["id"], app_b["id"]]
    assert all(r.stop_reason == worker.StopReason.AWAITING_SUBMIT_CONFIRMATION for r in summary.processed)


# 2. THE critical one: an unrelated QUEUED application outside the run is never touched
def test_run_worker_for_run_never_touches_unrelated_queued_application(fake_intervention_repo, page, monkeypatch):
    app_in_run = _make_queued_application("RUN-IN")
    app_outside = _make_queued_application("RUN-OUTSIDE")
    _patch_happy_path(monkeypatch)
    run = run_registry.create_run([app_in_run["id"]], candidate_id=CANDIDATE)

    worker.run_worker_for_run(page, run["id"], "test-worker")

    assert app_repo.get_application(app_outside["id"])["status"] == "QUEUED"
    assert app_repo.get_application(app_in_run["id"])["status"] != "QUEUED"


# 3. Run status transitions QUEUED -> RUNNING -> COMPLETE
def test_run_worker_for_run_updates_run_status_to_complete(fake_intervention_repo, page, monkeypatch):
    app_a = _make_queued_application("RUN-STATUS")
    _patch_happy_path(monkeypatch)
    run = run_registry.create_run([app_a["id"]], candidate_id=CANDIDATE)

    worker.run_worker_for_run(page, run["id"], "test-worker")

    assert run_registry.get_run(run["id"])["status"] == "COMPLETE"


# 4. Stop Run (checked before claiming the next id) prevents the next job from starting
def test_run_worker_for_run_stop_prevents_next_job(fake_intervention_repo, page, monkeypatch):
    app_a = _make_queued_application("RUN-STOP-A")
    app_b = _make_queued_application("RUN-STOP-B")
    _patch_happy_path(monkeypatch)
    run = run_registry.create_run([app_a["id"], app_b["id"]], candidate_id=CANDIDATE)

    real_process = worker.process_one_application

    def stop_after_first(*a, **k):
        result = real_process(*a, **k)
        run_registry.update_run_status(run["id"], "STOPPED")
        return result

    monkeypatch.setattr(worker, "process_one_application", stop_after_first)

    summary = worker.run_worker_for_run(page, run["id"], "test-worker")

    assert len(summary.processed) == 1
    assert summary.halted is True
    assert app_repo.get_application(app_b["id"])["status"] == "QUEUED"  # never claimed
    assert run_registry.get_run(run["id"])["status"] == "STOPPED"


# 5. A session-level stop halts the whole run immediately -- not after 3 like run_worker()'s circuit breaker
def test_run_worker_for_run_halts_immediately_on_session_level_stop(fake_intervention_repo, page, monkeypatch):
    app_a = _make_queued_application("RUN-AUTH-A")
    app_b = _make_queued_application("RUN-AUTH-B")
    monkeypatch.setattr(worker, "open_job", lambda page, url: _nav_result(authenticated=False))
    run = run_registry.create_run([app_a["id"], app_b["id"]], candidate_id=CANDIDATE)

    summary = worker.run_worker_for_run(page, run["id"], "test-worker")

    assert len(summary.processed) == 1
    assert summary.halted is True
    assert app_repo.get_application(app_b["id"])["status"] == "QUEUED"


# 6. NEEDS_INPUT on one job does not block the next independent selected job
def test_run_worker_for_run_needs_input_does_not_block_next_job(fake_intervention_repo, page, monkeypatch):
    from dice_browser.models import FieldType, QuestionField, QuestionStatus, RequiredState

    app_a = _make_queued_application("RUN-NI-A")
    app_b = _make_queued_application("RUN-NI-B")
    _patch_happy_path(monkeypatch, total_steps=3)
    question = QuestionField(
        question_id="q-1", prompt="Expected salary?", field_type=FieldType.TEXTAREA,
        required_state=RequiredState.UNKNOWN, options=None, current_value=None, helper=None, status=QuestionStatus.NEEDS_INPUT,
    )
    monkeypatch.setattr(worker, "extract_questions", lambda page: QuestionExtractionResult(QuestionExtractionStatus.QUESTIONS_PRESENT, (question,)))
    run = run_registry.create_run([app_a["id"], app_b["id"]], candidate_id=CANDIDATE)

    summary = worker.run_worker_for_run(page, run["id"], "test-worker")

    assert summary.processed[0].stop_reason == worker.StopReason.NEEDS_INPUT
    assert app_repo.get_application(app_a["id"])["status"] == "NEEDS_INPUT"
    # job 2 was still claimed and processed despite job 1 pausing
    assert len(summary.processed) == 2
    assert app_repo.get_application(app_b["id"])["status"] != "QUEUED"


# 7. An application that's no longer QUEUED by the time its turn comes is skipped, not reprocessed/re-submitted
def test_run_worker_for_run_skips_application_not_queued_anymore(fake_intervention_repo, page, monkeypatch):
    app_a = _make_queued_application("RUN-SKIP-A")
    _patch_happy_path(monkeypatch)
    run = run_registry.create_run([app_a["id"]], candidate_id=CANDIDATE)
    app_repo.update_application_status(app_a["id"], "PROCESSING")
    app_repo.update_application_status(app_a["id"], "FAILED", error_code="TEST", error_message="already terminal")

    summary = worker.run_worker_for_run(page, run["id"], "test-worker")

    assert len(summary.processed) == 1
    assert summary.processed[0].stop_reason == worker.StopReason.NOTHING_QUEUED
    assert app_repo.get_application(app_a["id"])["status"] == "FAILED"  # untouched, not re-processed
