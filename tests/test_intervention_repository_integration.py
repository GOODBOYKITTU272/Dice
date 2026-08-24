"""Phase 4F integration test against the real, linked Supabase project
(pkuqcnvtweukgurisczw) -- exercises NEEDS_INPUT -> resolved -> RESUMABLE
end to end against real Postgres, using the same TEST-prefixed disposable
row convention as test_application_repository_integration.py. Skips
automatically (see conftest.py::live_client) if SUPABASE_URL /
SUPABASE_SERVICE_ROLE_KEY aren't set or the schema isn't applied.

Never touches Dice.com -- this is Supabase-only, internal/test data.
"""
import uuid

import pytest

from db.application_repository import enqueue_application, get_application, update_application_status, upsert_dice_job
from db.intervention_repository import (
    ApplicationReadiness,
    compute_application_readiness,
    create_or_get_question_intervention,
    find_reusable_answer,
    resolve_question_intervention,
)
from db.supabase_client import get_supabase_client

ONSITE_QUESTION_ID = "c59c9cd9-8441-4610-8e13-2621ae1669c2"
ONSITE_PROMPT = "Are you able and willing to regularly come into the office to work?"

_created_job_ids = []


def _make_test_job():
    dice_job_id = f"TEST-{uuid.uuid4()}"
    job = upsert_dice_job(
        {
            "dice_job_id": dice_job_id,
            "canonical_url": f"https://dice.com/job/{dice_job_id}",
            "title": "Phase 4F Integration Test Role",
        }
    )
    _created_job_ids.append(job["id"])
    return job


def _cleanup(job_id: str):
    client = get_supabase_client()
    apps = client.table("applications").select("id").eq("dice_job_id", job_id).execute().data
    for a in apps:
        aid = a["id"]
        for iv in client.table("interventions").select("id").eq("application_id", aid).execute().data:
            client.table("interventions").delete().eq("id", iv["id"]).execute()
        for ev in client.table("application_events").select("id").eq("application_id", aid).execute().data:
            client.table("application_events").delete().eq("id", ev["id"]).execute()
        client.table("applications").delete().eq("id", aid).execute()
    client.table("dice_jobs").delete().eq("id", job_id).execute()


@pytest.fixture(autouse=True)
def _cleanup_created_jobs():
    try:
        yield
    finally:
        while _created_job_ids:
            _cleanup(_created_job_ids.pop())


def test_needs_input_resolved_resumable_end_to_end(live_client):
    candidate_id = str(uuid.uuid4())
    job = _make_test_job()
    app = enqueue_application(candidate_id, job["id"])
    update_application_status(app["id"], "PROCESSING", worker_id="test-worker-4f")

    assert compute_application_readiness(app["id"]) == ApplicationReadiness.RUNNING

    intervention = create_or_get_question_intervention(
        application_id=app["id"],
        question_id=ONSITE_QUESTION_ID,
        question_prompt=ONSITE_PROMPT,
        field_type="RADIO",
        reason="no trusted candidate field represents this exact job-specific onsite commitment",
        choices=["Yes", "No"],
        sensitive=False,
    )
    assert intervention["status"] == "OPEN"
    assert get_application(app["id"])["status"] == "NEEDS_INPUT"
    assert compute_application_readiness(app["id"]) == ApplicationReadiness.NEEDS_INPUT

    # Restart simulation: re-encountering the same question must recover
    # the same row, never create a duplicate.
    same_intervention = create_or_get_question_intervention(
        application_id=app["id"],
        question_id=ONSITE_QUESTION_ID,
        question_prompt=ONSITE_PROMPT,
        field_type="RADIO",
        reason="no trusted candidate field represents this exact job-specific onsite commitment",
        choices=["Yes", "No"],
        sensitive=False,
    )
    assert same_intervention["id"] == intervention["id"]

    resolved = resolve_question_intervention(intervention["id"], "Yes", source="human")
    assert resolved["status"] == "ANSWERED"
    assert resolved["answer"] == "Yes"

    assert compute_application_readiness(app["id"]) == ApplicationReadiness.RESUMABLE
    # RESUMABLE is derived, never persisted -- stored status is untouched
    assert get_application(app["id"])["status"] == "NEEDS_INPUT"


# ── find_reusable_answer (2026-08-24) ─────────────────────────────────────
# Real product request: don't re-ask a candidate the same standardized
# Dice question (e.g. "current city of residence") on every new job --
# reuse whatever they already explicitly answered once, without ever
# guessing or pulling from a candidate-profile inference.


def test_find_reusable_answer_returns_prior_answer_from_a_different_application(live_client):
    candidate_id = str(uuid.uuid4())
    job1 = _make_test_job()
    job2 = _make_test_job()
    app1 = enqueue_application(candidate_id, job1["id"])
    app2 = enqueue_application(candidate_id, job2["id"])
    update_application_status(app1["id"], "PROCESSING", worker_id="test-worker-reuse")

    intervention = create_or_get_question_intervention(
        application_id=app1["id"],
        question_id="candidateLocation",
        question_prompt="What is your current city of residence? *",
        field_type="TEXT_INPUT",
        reason="no trusted candidate mapping",
        sensitive=False,
    )
    resolve_question_intervention(intervention["id"], "West Haven, CT", source="operator")

    # app2 never had this question asked -- must still find app1's answer.
    assert find_reusable_answer(candidate_id, "candidateLocation") == "West Haven, CT"
    assert app2["id"]  # app2 exists solely to prove reuse crosses applications, not just re-reads the same row


def test_find_reusable_answer_never_crosses_candidates(live_client):
    candidate_a = str(uuid.uuid4())
    candidate_b = str(uuid.uuid4())
    job = _make_test_job()
    app_a = enqueue_application(candidate_a, job["id"])
    update_application_status(app_a["id"], "PROCESSING", worker_id="test-worker-reuse")

    intervention = create_or_get_question_intervention(
        application_id=app_a["id"],
        question_id="candidateLocation",
        question_prompt="What is your current city of residence? *",
        field_type="TEXT_INPUT",
        reason="no trusted candidate mapping",
        sensitive=False,
    )
    resolve_question_intervention(intervention["id"], "West Haven, CT", source="operator")

    assert find_reusable_answer(candidate_b, "candidateLocation") is None


def test_find_reusable_answer_never_reuses_work_authorization(live_client):
    # Deliberately excluded even though it's a stable, repeated
    # question_id -- work authorization/visa status is reconfirmed on
    # every application by design, never silently propagated.
    candidate_id = str(uuid.uuid4())
    job = _make_test_job()
    app = enqueue_application(candidate_id, job["id"])
    update_application_status(app["id"], "PROCESSING", worker_id="test-worker-reuse")

    intervention = create_or_get_question_intervention(
        application_id=app["id"],
        question_id="workAuthorization",
        question_prompt="Work Authorization *",
        field_type="SELECT",
        reason="no trusted candidate mapping",
        choices=["US Citizen", "Have H1 Visa"],
        sensitive=False,
    )
    resolve_question_intervention(intervention["id"], "Have H1 Visa", source="operator")

    assert find_reusable_answer(candidate_id, "workAuthorization") is None


def test_find_reusable_answer_returns_none_when_no_prior_answer(live_client):
    assert find_reusable_answer(str(uuid.uuid4()), "candidateLocation") is None


def test_find_reusable_answer_never_matches_a_different_question_id(live_client):
    # A job-specific custom question (fresh UUID per job posting) must
    # never accidentally match a differently-named question_id.
    candidate_id = str(uuid.uuid4())
    job = _make_test_job()
    app = enqueue_application(candidate_id, job["id"])
    update_application_status(app["id"], "PROCESSING", worker_id="test-worker-reuse")

    intervention = create_or_get_question_intervention(
        application_id=app["id"],
        question_id=ONSITE_QUESTION_ID,
        question_prompt=ONSITE_PROMPT,
        field_type="RADIO",
        reason="no trusted candidate field represents this exact job-specific onsite commitment",
        choices=["Yes", "No"],
        sensitive=False,
    )
    resolve_question_intervention(intervention["id"], "Yes", source="operator")

    assert find_reusable_answer(candidate_id, "a-different-jobs-onsite-question-uuid") is None
