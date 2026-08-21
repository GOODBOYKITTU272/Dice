"""Phase 6 integration test against the real, linked Supabase project
(pkuqcnvtweukgurisczw) -- exercises process_one_application()'s real
claim/lock/status-transition/intervention-creation behavior against real
Postgres (not the in-memory fake client), with the BROWSER layer mocked
(monkeypatched the same way as tests/test_worker.py) so this never
touches live Dice.com. Uses the same disposable TEST-prefixed row
convention as every other integration test in this project, cleaned up
after. Skips automatically if SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY
aren't set (see conftest.py::live_client).
"""
from __future__ import annotations

import uuid

import pytest
from playwright.sync_api import sync_playwright

import db.application_repository as app_repo
import dice_browser.worker as worker
from dice.models import CandidateFetchResult, CandidateFetchStatus, CandidateProfile
from dice_browser.models import BrowserState, EasyApplyOpenResult, NavigationResult, QuestionExtractionResult, QuestionExtractionStatus


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


def _make_test_job():
    dice_job_id = f"TEST-{uuid.uuid4()}"
    return app_repo.upsert_dice_job(
        {
            "dice_job_id": dice_job_id,
            "canonical_url": f"https://dice.com/job-detail/{dice_job_id}",
            "title": "Phase 6 Worker Integration Test Role",
        }
    )


def _patch_no_question_happy_path(monkeypatch):
    state = {"step": 1}
    monkeypatch.setattr(
        worker,
        "open_job",
        lambda page, url: NavigationResult(url, "title", BrowserState.ACTIVE, True, False, True, None, "test"),
    )
    monkeypatch.setattr(worker, "open_easy_apply", lambda page, nav: EasyApplyOpenResult(True, "url", "title", "OPENED"))
    monkeypatch.setattr(worker, "detect_existing_resume", lambda page: True)
    monkeypatch.setattr(worker, "is_review_screen", lambda page: state["step"] >= 2)
    monkeypatch.setattr(worker, "extract_questions", lambda page: QuestionExtractionResult(QuestionExtractionStatus.NO_QUESTIONS_PRESENT, ()))

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


def _cleanup(job_id: str):
    apps = app_repo.get_supabase_client().table("applications").select("id").eq("dice_job_id", job_id).execute().data
    for a in apps:
        aid = a["id"]
        for iv in app_repo.get_supabase_client().table("interventions").select("id").eq("application_id", aid).execute().data:
            app_repo.get_supabase_client().table("interventions").delete().eq("id", iv["id"]).execute()
        for ev in app_repo.get_supabase_client().table("application_events").select("id").eq("application_id", aid).execute().data:
            app_repo.get_supabase_client().table("application_events").delete().eq("id", ev["id"]).execute()
        app_repo.get_supabase_client().table("applications").delete().eq("id", aid).execute()
    app_repo.get_supabase_client().table("dice_jobs").delete().eq("id", job_id).execute()


def test_process_one_application_against_real_supabase(live_client, page, monkeypatch):
    candidate_id = str(uuid.uuid4())
    job = _make_test_job()
    app_repo.enqueue_application(candidate_id, job["id"])
    _patch_no_question_happy_path(monkeypatch)

    try:
        result = worker.process_one_application(page, candidate_id, "test-worker-6")
        assert result.stop_reason == worker.StopReason.AWAITING_SUBMIT_CONFIRMATION

        application = app_repo.get_application(result.application_id)
        # Real bug found via live-Supabase bounded-run testing (2026-08-22,
        # see dice_browser/worker.py's _gate_and_maybe_submit): staying at
        # PROCESSING forever would permanently block the claim RPC's "no
        # other PROCESSING/SUBMITTING application for this candidate" gate
        # for every later application. Transitions to NEEDS_INPUT instead --
        # the existing, already-modeled "doesn't block sibling claims"
        # status -- with no interventions row (nothing to answer, just
        # awaiting a human's Submit go/no-go).
        assert application["status"] == "NEEDS_INPUT"
        assert application["worker_id"] == "test-worker-6"

        events = (
            app_repo.get_supabase_client()
            .table("application_events")
            .select("*")
            .eq("application_id", result.application_id)
            .execute()
            .data
        )
        assert any(e["event_type"] == "easy_apply_opened" for e in events)
        assert any(e["event_type"] == "awaiting_submit_confirmation" for e in events)

        # Idempotency: claiming again finds nothing QUEUED left for this candidate.
        second = worker.process_one_application(page, candidate_id, "test-worker-6")
        assert second.stop_reason == worker.StopReason.NOTHING_QUEUED
    finally:
        _cleanup(job["id"])


def test_process_one_application_needs_input_creates_real_intervention(live_client, page, monkeypatch):
    candidate_id = str(uuid.uuid4())
    job = _make_test_job()
    app_repo.enqueue_application(candidate_id, job["id"])
    _patch_no_question_happy_path(monkeypatch)

    from dice_browser.models import FieldType, QuestionField, QuestionStatus, RequiredState

    question = QuestionField(
        question_id="onsite-q-uuid", prompt="Are you able and willing to regularly come into the office to work?",
        field_type=FieldType.RADIO, required_state=RequiredState.UNKNOWN, options=("Yes", "No"),
        current_value=None, helper=None, status=QuestionStatus.NEEDS_INPUT,
    )
    monkeypatch.setattr(
        worker, "extract_questions", lambda page: QuestionExtractionResult(QuestionExtractionStatus.QUESTIONS_PRESENT, (question,))
    )
    monkeypatch.setattr(worker, "is_review_screen", lambda page: False)  # never reaches Review in this test

    try:
        result = worker.process_one_application(page, candidate_id, "test-worker-6")
        assert result.stop_reason == worker.StopReason.NEEDS_INPUT

        application = app_repo.get_application(result.application_id)
        assert application["status"] == "NEEDS_INPUT"

        interventions = (
            app_repo.get_supabase_client()
            .table("interventions")
            .select("*")
            .eq("application_id", result.application_id)
            .execute()
            .data
        )
        assert len(interventions) == 1
        assert interventions[0]["options"]["question_id"] == "onsite-q-uuid"

        # Idempotency: re-processing the same NEEDS_INPUT application via
        # resume_needs_input_application before it's resolved must not
        # create a duplicate intervention.
        readiness_check = worker.resume_needs_input_application(page, result.application_id, "test-worker-6")
        assert readiness_check.stop_reason == worker.StopReason.NEEDS_INPUT
        interventions_after = (
            app_repo.get_supabase_client()
            .table("interventions")
            .select("*")
            .eq("application_id", result.application_id)
            .execute()
            .data
        )
        assert len(interventions_after) == 1
    finally:
        _cleanup(job["id"])
