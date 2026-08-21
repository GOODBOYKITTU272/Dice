"""Phase 4F integration test against the real, linked Supabase project
(pkuqcnvtweukgurisczw) -- exercises NEEDS_INPUT -> resolved -> RESUMABLE
end to end against real Postgres, using the same TEST-prefixed disposable
row convention as test_application_repository_integration.py. Skips
automatically (see conftest.py::live_client) if SUPABASE_URL /
SUPABASE_SERVICE_ROLE_KEY aren't set or the schema isn't applied.

Never touches Dice.com -- this is Supabase-only, internal/test data.
"""
import uuid

from db.application_repository import enqueue_application, get_application, update_application_status, upsert_dice_job
from db.intervention_repository import ApplicationReadiness, compute_application_readiness, create_or_get_question_intervention, resolve_question_intervention

ONSITE_QUESTION_ID = "c59c9cd9-8441-4610-8e13-2621ae1669c2"
ONSITE_PROMPT = "Are you able and willing to regularly come into the office to work?"


def _make_test_job():
    dice_job_id = f"TEST-{uuid.uuid4()}"
    return upsert_dice_job(
        {
            "dice_job_id": dice_job_id,
            "canonical_url": f"https://dice.com/job/{dice_job_id}",
            "title": "Phase 4F Integration Test Role",
        }
    )


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
