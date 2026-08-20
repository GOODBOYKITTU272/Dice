"""Integration tests against the real, linked Supabase project
(pkuqcnvtweukgurisczw). One test per required Phase 1 live-verification
behavior — see STATE.md / the Phase 1 final verification report for the
numbered list this file is checked against.

These exercise behavior that can't be honestly faked in-process: real
unique-constraint enforcement and the real atomic claim under Postgres'
FOR UPDATE SKIP LOCKED, including true concurrent callers. They skip
automatically (see conftest.py::live_client) if SUPABASE_URL /
SUPABASE_SERVICE_ROLE_KEY aren't set or the schema isn't applied yet.

Every row uses a per-test UUID candidate/job id so repeat runs don't
collide with each other's leftovers. This file only creates and reads
rows — it never resets or drops schema.
"""
import uuid
from concurrent.futures import ThreadPoolExecutor

from db.application_repository import (
    DuplicateApplicationError,
    add_event,
    claim_next_queued_application,
    create_intervention,
    enqueue_application,
    requeue_failed_application,
    resolve_intervention,
    update_application_status,
    upsert_dice_job,
)


def _unique_job_id():
    return f"TEST-{uuid.uuid4()}"


def _make_job(**overrides):
    dice_job_id = _unique_job_id()
    job = {
        "dice_job_id": dice_job_id,
        "canonical_url": f"https://dice.com/job/{dice_job_id}",
        "title": "Integration Test Role",
    }
    job.update(overrides)
    return upsert_dice_job(job)


# 1. Dice job upsert is idempotent.
def test_dice_job_upsert_is_idempotent(live_client):
    dice_job_id = _unique_job_id()
    job = {
        "dice_job_id": dice_job_id,
        "canonical_url": f"https://dice.com/job/{dice_job_id}",
        "title": "Integration Test Contract Role",
    }
    first = upsert_dice_job(job)
    second = upsert_dice_job({**job, "title": "Integration Test Contract Role v2"})
    assert first["id"] == second["id"]


# 2. Duplicate candidate_id + dice_job_id application enqueue is rejected.
def test_duplicate_application_enqueue_is_rejected(live_client):
    candidate_id = str(uuid.uuid4())
    job = _make_job()

    enqueue_application(candidate_id, job["id"])
    try:
        enqueue_application(candidate_id, job["id"])
        assert False, "expected DuplicateApplicationError from DB constraint"
    except DuplicateApplicationError:
        pass


# 3. Oldest eligible QUEUED application is claimed.
def test_oldest_eligible_queued_application_is_claimed(live_client):
    candidate_id = str(uuid.uuid4())
    job_a = _make_job()
    job_b = _make_job()
    app_a = enqueue_application(candidate_id, job_a["id"])
    enqueue_application(candidate_id, job_b["id"])

    claimed = claim_next_queued_application(candidate_id, "worker-integration")
    assert claimed is not None
    assert claimed["id"] == app_a["id"], "must claim the first-enqueued (oldest queued_at) row"


# 4. Two concurrent workers cannot create two active applications for the
#    same candidate — real threads racing the real RPC, not a sequential
#    call/call pattern.
def test_concurrent_claims_yield_only_one_active_application(live_client):
    candidate_id = str(uuid.uuid4())
    job_a = _make_job()
    job_b = _make_job()
    enqueue_application(candidate_id, job_a["id"])
    enqueue_application(candidate_id, job_b["id"])

    def _claim(worker_id):
        return claim_next_queued_application(candidate_id, worker_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(_claim, ["worker-x", "worker-y"]))

    successes = [r for r in results if r is not None]
    assert len(successes) == 1, f"expected exactly one concurrent claim to win, got {len(successes)}"


# 5. PROCESSING blocks another claim.
def test_processing_blocks_another_claim(live_client):
    candidate_id = str(uuid.uuid4())
    job_a = _make_job()
    job_b = _make_job()
    enqueue_application(candidate_id, job_a["id"])
    enqueue_application(candidate_id, job_b["id"])

    first = claim_next_queued_application(candidate_id, "worker-integration")
    assert first is not None and first["status"] == "PROCESSING"

    blocked = claim_next_queued_application(candidate_id, "worker-integration")
    assert blocked is None


# 6. SUBMITTING blocks another claim.
def test_submitting_blocks_another_claim(live_client):
    candidate_id = str(uuid.uuid4())
    job_a = _make_job()
    job_b = _make_job()
    app_a = enqueue_application(candidate_id, job_a["id"])
    enqueue_application(candidate_id, job_b["id"])

    update_application_status(app_a["id"], "PROCESSING")
    update_application_status(app_a["id"], "SUBMITTING")

    blocked = claim_next_queued_application(candidate_id, "worker-integration")
    assert blocked is None, "SUBMITTING must block a new claim just like PROCESSING"


# 7. APPLICATION_LEVEL NEEDS_INPUT does not block another independent job.
def test_application_level_needs_input_does_not_block_next_claim(live_client):
    candidate_id = str(uuid.uuid4())
    job_a = _make_job()
    job_b = _make_job()
    app_a = enqueue_application(candidate_id, job_a["id"])
    enqueue_application(candidate_id, job_b["id"])

    update_application_status(app_a["id"], "PROCESSING")
    create_intervention(app_a["id"], "UNKNOWN_QUESTION", "APPLICATION_LEVEL")

    claimed = claim_next_queued_application(candidate_id, "worker-integration")
    assert claimed is not None
    assert claimed["dice_job_id"] == job_b["id"]


# 8. OPEN SESSION_LEVEL intervention blocks all new claims for that candidate.
def test_open_session_level_intervention_blocks_all_claims(live_client):
    candidate_id = str(uuid.uuid4())
    job_a = _make_job()
    job_b = _make_job()
    app_a = enqueue_application(candidate_id, job_a["id"])
    enqueue_application(candidate_id, job_b["id"])

    update_application_status(app_a["id"], "PROCESSING")
    create_intervention(app_a["id"], "SECURITY_ACTION", "SESSION_LEVEL")

    claimed = claim_next_queued_application(candidate_id, "worker-integration")
    assert claimed is None, "SESSION_LEVEL intervention must block any new claim"


# 9. Resolving the SESSION_LEVEL intervention allows claims again.
def test_resolving_session_level_intervention_allows_claims_again(live_client):
    candidate_id = str(uuid.uuid4())
    job_a = _make_job()
    job_b = _make_job()
    app_a = enqueue_application(candidate_id, job_a["id"])
    enqueue_application(candidate_id, job_b["id"])

    update_application_status(app_a["id"], "PROCESSING")
    intervention = create_intervention(app_a["id"], "SECURITY_ACTION", "SESSION_LEVEL")

    still_blocked = claim_next_queued_application(candidate_id, "worker-integration")
    assert still_blocked is None

    resolve_intervention(intervention["id"], {"resolved": True}, answered_by="test-operator")

    claimed = claim_next_queued_application(candidate_id, "worker-integration")
    assert claimed is not None
    assert claimed["dice_job_id"] == job_b["id"]


# 10 & 11. FAILED_RETRYABLE can be explicitly requeued, and requeue
#          increments attempt_count.
def test_requeue_failed_application_and_attempt_count(live_client):
    candidate_id = str(uuid.uuid4())
    job = _make_job()
    app = enqueue_application(candidate_id, job["id"])
    assert app["attempt_count"] == 0

    update_application_status(app["id"], "PROCESSING")
    update_application_status(app["id"], "FAILED_RETRYABLE")

    requeued = requeue_failed_application(app["id"])
    assert requeued["status"] == "QUEUED"
    assert requeued["attempt_count"] == 1, "requeue must increment attempt_count"

    claimed = claim_next_queued_application(candidate_id, "worker-integration")
    assert claimed is not None
    assert claimed["id"] == app["id"]


# 12. Application event creation works.
def test_event_creation_persists(live_client):
    candidate_id = str(uuid.uuid4())
    job = _make_job()
    app = enqueue_application(candidate_id, job["id"])

    event = add_event(app["id"], "worker_claimed", step="OPEN_JOB", message="test event")
    assert event["application_id"] == app["id"]
    assert event["event_type"] == "worker_claimed"
