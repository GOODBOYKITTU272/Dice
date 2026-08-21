"""Phase 4F: NEEDS_INPUT pause/resume orchestration. Run against the same
in-memory fake Supabase client as Phase 1's repository tests (see
tests/conftest.py::fake_intervention_repo) -- no live project required.

Question fixtures below are the two real, live-observed Dice questions
from Phase 4D (Java Developer @ Yashnee Tech Solutions,
job 3f63223a-1dc9-4af9-914c-4ed01e625d44, 2026-08-21):
  - "Are you able and willing to regularly come into the office to work?"
    (RADIO, Yes/No)
  - "What is your expected rate or salary?" (TEXTAREA)
Both classify NEEDS_INPUT in Phase 4D -- no trusted candidate field maps
to either. The "sensitive" fixture question below is NOT live-observed
(no real sensitive question has been seen on Dice yet, per Phase 4D-A's
finding that Work Authorization/Current Location are display-only
summaries, not questions) -- it's a simulated shape used only to prove
the sensitivity flag survives the round trip, clearly labeled as such.
"""
from __future__ import annotations

import db.application_repository as app_repo
from db.application_repository import InvalidStatusTransitionError
from db.intervention_repository import (
    AlreadyResolvedError,
    ApplicationReadiness,
    InvalidAnswerError,
    compute_application_readiness,
    create_or_get_question_intervention,
    resolve_question_intervention,
)

CANDIDATE = "11111111-1111-1111-1111-111111111111"
OTHER_CANDIDATE = "22222222-2222-2222-2222-222222222222"

ONSITE_QUESTION_ID = "c59c9cd9-8441-4610-8e13-2621ae1669c2"
ONSITE_PROMPT = "Are you able and willing to regularly come into the office to work?"
SALARY_QUESTION_ID = "96824b6c-c489-4500-9dcc-d82847b7b1b3"
SALARY_PROMPT = "What is your expected rate or salary?"


def _make_processing_application(repo_module, dice_job_id="DICE-4F-1", candidate_id=CANDIDATE):
    job = repo_module.upsert_dice_job(
        {
            "dice_job_id": dice_job_id,
            "canonical_url": f"https://dice.com/job/{dice_job_id}",
            "title": "Java Developer with 8+ experience",
        }
    )
    app = repo_module.enqueue_application(candidate_id, job["id"])
    repo_module.update_application_status(app["id"], "PROCESSING", worker_id="test-worker")
    return app


def _create_onsite_intervention(iv_repo, application_id):
    return create_or_get_question_intervention(
        application_id=application_id,
        question_id=ONSITE_QUESTION_ID,
        question_prompt=ONSITE_PROMPT,
        field_type="RADIO",
        reason="no trusted candidate field represents this exact job-specific onsite commitment",
        choices=["Yes", "No"],
        sensitive=False,
    )


def _create_salary_intervention(iv_repo, application_id):
    return create_or_get_question_intervention(
        application_id=application_id,
        question_id=SALARY_QUESTION_ID,
        question_prompt=SALARY_PROMPT,
        field_type="TEXTAREA",
        reason="no trusted compensation preference exists",
        choices=None,
        sensitive=False,
    )


# 1. NEEDS_INPUT creates intervention
def test_needs_input_creates_intervention_and_blocks_application(fake_intervention_repo):
    app = _make_processing_application(app_repo)
    row = _create_onsite_intervention(fake_intervention_repo, app["id"])

    assert row["status"] == "OPEN"
    assert row["question_text"] == ONSITE_PROMPT
    updated_app = app_repo.get_application(app["id"])
    assert updated_app["status"] == "NEEDS_INPUT"


# 2. SENSITIVE_NEEDS_INPUT creates intervention
def test_sensitive_needs_input_creates_intervention(fake_intervention_repo):
    app = _make_processing_application(app_repo)
    row = create_or_get_question_intervention(
        application_id=app["id"],
        question_id="simulated-sensitive-question-uuid",
        question_prompt="Are you authorized to work without sponsorship?",
        field_type="RADIO",
        reason="sensitive legal/work-authorization disclosure -- never auto-answered",
        choices=["Yes", "No"],
        sensitive=True,
    )
    assert row["status"] == "OPEN"
    assert row["options"]["sensitivity"] is True


# 3. UNSUPPORTED creates intervention
def test_unsupported_creates_intervention(fake_intervention_repo):
    app = _make_processing_application(app_repo)
    row = create_or_get_question_intervention(
        application_id=app["id"],
        question_id="some-select-question-uuid",
        question_prompt="Which office location do you prefer?",
        field_type="UNSUPPORTED",
        reason="SELECT control type not yet supported by the extractor",
        choices=None,
        sensitive=False,
    )
    assert row["status"] == "OPEN"
    assert row["type"] == "UNKNOWN_QUESTION"


# 4. duplicate worker restart does not duplicate unresolved intervention
def test_restart_does_not_duplicate_unresolved_intervention(fake_intervention_repo):
    app = _make_processing_application(app_repo)
    first = _create_onsite_intervention(fake_intervention_repo, app["id"])
    second = _create_onsite_intervention(fake_intervention_repo, app["id"])  # simulates a worker restart

    assert first["id"] == second["id"]
    client = app_repo.get_supabase_client()
    matching = [r for r in client.tables["interventions"] if r["application_id"] == app["id"]]
    assert len(matching) == 1


# 5. two different questions create two interventions
def test_two_different_questions_create_two_interventions(fake_intervention_repo):
    app = _make_processing_application(app_repo)
    onsite = _create_onsite_intervention(fake_intervention_repo, app["id"])
    salary = _create_salary_intervention(fake_intervention_repo, app["id"])

    assert onsite["id"] != salary["id"]
    client = app_repo.get_supabase_client()
    matching = [r for r in client.tables["interventions"] if r["application_id"] == app["id"]]
    assert len(matching) == 2


# 6. same question on different applications stays distinct
def test_same_question_on_different_applications_stays_distinct(fake_intervention_repo):
    app_a = _make_processing_application(app_repo, dice_job_id="DICE-4F-A", candidate_id=CANDIDATE)
    app_b = _make_processing_application(app_repo, dice_job_id="DICE-4F-B", candidate_id=OTHER_CANDIDATE)

    row_a = _create_onsite_intervention(fake_intervention_repo, app_a["id"])
    row_b = _create_onsite_intervention(fake_intervention_repo, app_b["id"])

    assert row_a["id"] != row_b["id"]
    assert row_a["application_id"] != row_b["application_id"]


# 7. unresolved intervention blocks application progress
def test_unresolved_intervention_keeps_application_needs_input(fake_intervention_repo):
    app = _make_processing_application(app_repo)
    _create_onsite_intervention(fake_intervention_repo, app["id"])

    readiness = compute_application_readiness(app["id"])
    assert readiness == ApplicationReadiness.NEEDS_INPUT


# 8. valid human answer resolves intervention
def test_valid_answer_resolves_intervention(fake_intervention_repo):
    app = _make_processing_application(app_repo)
    row = _create_onsite_intervention(fake_intervention_repo, app["id"])

    resolved = resolve_question_intervention(row["id"], "Yes", source="human")
    assert resolved["status"] == "ANSWERED"
    assert resolved["answer"] == "Yes"
    assert resolved["answered_by"] == "human"
    assert resolved["resolved_at"] is not None


# 9. invalid radio option rejected
def test_invalid_radio_option_rejected(fake_intervention_repo):
    app = _make_processing_application(app_repo)
    row = _create_onsite_intervention(fake_intervention_repo, app["id"])

    try:
        resolve_question_intervention(row["id"], "Maybe", source="human")
        assert False, "expected InvalidAnswerError"
    except InvalidAnswerError:
        pass

    client = app_repo.get_supabase_client()
    unchanged = next(r for r in client.tables["interventions"] if r["id"] == row["id"])
    assert unchanged["status"] == "OPEN"


# 10. free-text answer preserved verbatim
def test_free_text_answer_preserved_verbatim(fake_intervention_repo):
    app = _make_processing_application(app_repo)
    row = _create_salary_intervention(fake_intervention_repo, app["id"])

    verbatim_answer = "  $95/hr, negotiable for the right contract length  "
    resolved = resolve_question_intervention(row["id"], verbatim_answer, source="human")
    assert resolved["answer"] == verbatim_answer  # not stripped, not rewritten


# 11. resolved intervention is not resolved twice silently
def test_already_resolved_intervention_raises_on_second_resolve(fake_intervention_repo):
    app = _make_processing_application(app_repo)
    row = _create_onsite_intervention(fake_intervention_repo, app["id"])
    resolve_question_intervention(row["id"], "Yes", source="human")

    try:
        resolve_question_intervention(row["id"], "No", source="human")
        assert False, "expected AlreadyResolvedError"
    except AlreadyResolvedError:
        pass

    client = app_repo.get_supabase_client()
    unchanged = next(r for r in client.tables["interventions"] if r["id"] == row["id"])
    assert unchanged["answer"] == "Yes"  # first answer preserved, not overwritten


# 12. resolved intervention makes application RESUMABLE when no blockers remain
def test_resolving_only_blocker_makes_application_resumable(fake_intervention_repo):
    app = _make_processing_application(app_repo)
    row = _create_onsite_intervention(fake_intervention_repo, app["id"])

    assert compute_application_readiness(app["id"]) == ApplicationReadiness.NEEDS_INPUT
    resolve_question_intervention(row["id"], "Yes", source="human")
    assert compute_application_readiness(app["id"]) == ApplicationReadiness.RESUMABLE

    # the underlying stored status is untouched -- RESUMABLE is derived,
    # never written to applications.status (no such value in the schema)
    assert app_repo.get_application(app["id"])["status"] == "NEEDS_INPUT"


# 13. one unresolved of two keeps application NEEDS_INPUT
def test_one_unresolved_of_two_keeps_needs_input(fake_intervention_repo):
    app = _make_processing_application(app_repo)
    onsite = _create_onsite_intervention(fake_intervention_repo, app["id"])
    _create_salary_intervention(fake_intervention_repo, app["id"])

    resolve_question_intervention(onsite["id"], "Yes", source="human")
    assert compute_application_readiness(app["id"]) == ApplicationReadiness.NEEDS_INPUT


# 14. sensitive flag preserved
def test_sensitive_flag_preserved_through_resolution(fake_intervention_repo):
    app = _make_processing_application(app_repo)
    row = create_or_get_question_intervention(
        application_id=app["id"],
        question_id="simulated-sensitive-question-uuid",
        question_prompt="Are you authorized to work without sponsorship?",
        field_type="RADIO",
        reason="sensitive legal/work-authorization disclosure",
        choices=["Yes", "No"],
        sensitive=True,
    )
    resolved = resolve_question_intervention(row["id"], "Yes", source="human")
    assert resolved["options"]["sensitivity"] is True


# 15. question_id preserved
def test_question_id_preserved(fake_intervention_repo):
    app = _make_processing_application(app_repo)
    row = _create_onsite_intervention(fake_intervention_repo, app["id"])
    assert row["options"]["question_id"] == ONSITE_QUESTION_ID


# 16. candidate_id/application_id/dice_job_id preserved
def test_candidate_and_job_identity_reachable_via_application(fake_intervention_repo):
    # candidate_id/dice_job_id are intentionally NOT duplicated onto the
    # intervention row -- they already live on the parent applications
    # row (its own candidate_id column and dice_job_id FK), so this
    # confirms the relationship is preserved via that existing link
    # rather than re-verifying data that was never copied in the first
    # place.
    app = _make_processing_application(app_repo, dice_job_id="DICE-4F-IDENTITY")
    row = _create_onsite_intervention(fake_intervention_repo, app["id"])

    parent_application = app_repo.get_application(row["application_id"])
    assert parent_application["id"] == app["id"]
    assert parent_application["candidate_id"] == CANDIDATE
    assert parent_application["dice_job_id"] == app["dice_job_id"]


# 19. restart/reload recovers unresolved intervention
def test_restart_recovers_the_same_open_intervention(fake_intervention_repo):
    app = _make_processing_application(app_repo)
    before_restart = _create_onsite_intervention(fake_intervention_repo, app["id"])

    # Simulate a fresh process re-discovering the same question after a
    # restart -- must recover the existing row, not create a new one and
    # not lose track of the one already open.
    after_restart = _create_onsite_intervention(fake_intervention_repo, app["id"])
    assert after_restart["id"] == before_restart["id"]
    assert after_restart["status"] == "OPEN"


# 20. application event written for NEEDS_INPUT transition
def test_event_written_for_needs_input_transition(fake_intervention_repo):
    app = _make_processing_application(app_repo)
    _create_onsite_intervention(fake_intervention_repo, app["id"])

    client = app_repo.get_supabase_client()
    events = [e for e in client.tables["application_events"] if e["application_id"] == app["id"]]
    needs_input_events = [e for e in events if e["event_type"] == "needs_input"]
    assert len(needs_input_events) == 1
    assert needs_input_events[0]["metadata"]["question_id"] == ONSITE_QUESTION_ID


# 21. application event written for resolution
def test_event_written_for_resolution(fake_intervention_repo):
    app = _make_processing_application(app_repo)
    row = _create_onsite_intervention(fake_intervention_repo, app["id"])
    resolve_question_intervention(row["id"], "Yes", source="human")

    client = app_repo.get_supabase_client()
    events = [e for e in client.tables["application_events"] if e["application_id"] == app["id"]]
    resolved_events = [e for e in events if e["event_type"] == "intervention_resolved"]
    assert len(resolved_events) == 1
    assert resolved_events[0]["metadata"]["question_id"] == ONSITE_QUESTION_ID
    assert resolved_events[0]["metadata"]["source"] == "human"


# ── application readiness across the full status range ───────────────────


def test_readiness_ready_running_submitted_failed(fake_intervention_repo):
    job = app_repo.upsert_dice_job(
        {"dice_job_id": "DICE-4F-STATES", "canonical_url": "https://dice.com/job/DICE-4F-STATES", "title": "x"}
    )
    app = app_repo.enqueue_application(CANDIDATE, job["id"])
    assert compute_application_readiness(app["id"]) == ApplicationReadiness.READY

    app_repo.update_application_status(app["id"], "PROCESSING")
    assert compute_application_readiness(app["id"]) == ApplicationReadiness.RUNNING

    app_repo.update_application_status(app["id"], "SUBMITTING")
    assert compute_application_readiness(app["id"]) == ApplicationReadiness.RUNNING

    app_repo.update_application_status(app["id"], "SUBMITTED")
    assert compute_application_readiness(app["id"]) == ApplicationReadiness.SUBMITTED


def test_readiness_failed(fake_intervention_repo):
    job = app_repo.upsert_dice_job(
        {"dice_job_id": "DICE-4F-FAILED", "canonical_url": "https://dice.com/job/DICE-4F-FAILED", "title": "x"}
    )
    app = app_repo.enqueue_application(CANDIDATE, job["id"])
    app_repo.update_application_status(app["id"], "PROCESSING")
    app_repo.update_application_status(app["id"], "FAILED")
    assert compute_application_readiness(app["id"]) == ApplicationReadiness.FAILED
