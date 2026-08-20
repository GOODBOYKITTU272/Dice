"""Repository-layer logic tests. Run against the in-memory fake Supabase
client (tests/conftest.py::fake_repo) — no live project required, matches
Phase 1's "do not require Dice.com for these tests" instruction, extended
here to also not require a live Postgres connection for pure logic checks.

Concurrency behavior of the atomic claim (real FOR UPDATE SKIP LOCKED) is
covered separately in test_application_repository_integration.py against
the live linked project, since that can't be meaningfully faked in-process.
"""
from db.application_repository import (
    DuplicateApplicationError,
    InvalidInterventionScopeError,
    InvalidStatusTransitionError,
)

CANDIDATE = "11111111-1111-1111-1111-111111111111"
OTHER_CANDIDATE = "22222222-2222-2222-2222-222222222222"


def _make_job(fake_repo, dice_job_id="DICE-1", title="Contract Engineer"):
    return fake_repo.upsert_dice_job(
        {
            "dice_job_id": dice_job_id,
            "canonical_url": f"https://dice.com/job/{dice_job_id}",
            "title": title,
        }
    )


def test_dice_job_upsert_is_idempotent_on_dice_job_id(fake_repo):
    first = _make_job(fake_repo, title="Contract Engineer")
    second = _make_job(fake_repo, title="Contract Engineer II")

    client = fake_repo.get_supabase_client()
    assert len(client.tables["dice_jobs"]) == 1
    assert first["id"] == second["id"]
    assert client.tables["dice_jobs"][0]["title"] == "Contract Engineer II"


def test_duplicate_application_enqueue_raises(fake_repo):
    job = _make_job(fake_repo)
    fake_repo.enqueue_application(CANDIDATE, job["id"])

    try:
        fake_repo.enqueue_application(CANDIDATE, job["id"])
        assert False, "expected DuplicateApplicationError"
    except DuplicateApplicationError:
        pass

    client = fake_repo.get_supabase_client()
    assert len(client.tables["applications"]) == 1


def test_valid_status_transition_allowed(fake_repo):
    job = _make_job(fake_repo)
    app = fake_repo.enqueue_application(CANDIDATE, job["id"])

    updated = fake_repo.update_application_status(app["id"], "PROCESSING")
    assert updated["status"] == "PROCESSING"


def test_invalid_status_transition_rejected(fake_repo):
    job = _make_job(fake_repo)
    app = fake_repo.enqueue_application(CANDIDATE, job["id"])

    try:
        fake_repo.update_application_status(app["id"], "SUBMITTED")
        assert False, "expected InvalidStatusTransitionError"
    except InvalidStatusTransitionError:
        pass

    unchanged = fake_repo.get_application(app["id"])
    assert unchanged["status"] == "QUEUED"


def test_needs_input_is_not_collapsed_to_failed(fake_repo):
    job = _make_job(fake_repo)
    app = fake_repo.enqueue_application(CANDIDATE, job["id"])
    fake_repo.update_application_status(app["id"], "PROCESSING")

    fake_repo.create_intervention(
        app["id"],
        "UNKNOWN_QUESTION",
        "APPLICATION_LEVEL",
        question_text="Are you willing to relocate?",
    )

    result = fake_repo.get_application(app["id"])
    assert result["status"] == "NEEDS_INPUT"


def test_invalid_intervention_scope_rejected(fake_repo):
    job = _make_job(fake_repo)
    app = fake_repo.enqueue_application(CANDIDATE, job["id"])

    try:
        fake_repo.create_intervention(app["id"], "UNKNOWN_QUESTION", "GLOBAL_LEVEL")
        assert False, "expected InvalidInterventionScopeError"
    except InvalidInterventionScopeError:
        pass


def test_application_level_needs_input_does_not_block_next_claim(fake_repo):
    job_a = _make_job(fake_repo, dice_job_id="DICE-A")
    job_b = _make_job(fake_repo, dice_job_id="DICE-B")
    app_a = fake_repo.enqueue_application(CANDIDATE, job_a["id"])
    fake_repo.enqueue_application(CANDIDATE, job_b["id"])

    fake_repo.update_application_status(app_a["id"], "PROCESSING")
    fake_repo.create_intervention(app_a["id"], "UNKNOWN_QUESTION", "APPLICATION_LEVEL")

    # app_a is now NEEDS_INPUT (non-active), so the worker may claim job_b.
    claimed = fake_repo.claim_next_queued_application(CANDIDATE, "worker-1")
    assert claimed is not None
    assert claimed["dice_job_id"] == job_b["id"]


def test_session_level_intervention_blocks_next_claim(fake_repo):
    job_a = _make_job(fake_repo, dice_job_id="DICE-A")
    job_b = _make_job(fake_repo, dice_job_id="DICE-B")
    app_a = fake_repo.enqueue_application(CANDIDATE, job_a["id"])
    fake_repo.enqueue_application(CANDIDATE, job_b["id"])

    fake_repo.update_application_status(app_a["id"], "PROCESSING")
    fake_repo.create_intervention(app_a["id"], "SECURITY_ACTION", "SESSION_LEVEL")

    # Session is unsafe to keep using — no new claim until this is resolved,
    # even though app_a itself is no longer PROCESSING/SUBMITTING.
    claimed = fake_repo.claim_next_queued_application(CANDIDATE, "worker-1")
    assert claimed is None


def test_claim_returns_none_when_nothing_queued(fake_repo):
    claimed = fake_repo.claim_next_queued_application(CANDIDATE, "worker-1")
    assert claimed is None


def test_claim_takes_oldest_queued_and_blocks_second_active_claim(fake_repo):
    job_a = _make_job(fake_repo, dice_job_id="DICE-A")
    job_b = _make_job(fake_repo, dice_job_id="DICE-B")
    fake_repo.enqueue_application(CANDIDATE, job_a["id"])
    fake_repo.enqueue_application(CANDIDATE, job_b["id"])

    first_claim = fake_repo.claim_next_queued_application(CANDIDATE, "worker-1")
    assert first_claim is not None
    assert first_claim["status"] == "PROCESSING"

    second_claim = fake_repo.claim_next_queued_application(CANDIDATE, "worker-1")
    assert second_claim is None, "must not claim a second job while one is PROCESSING"


def test_claim_is_independent_per_candidate(fake_repo):
    job = _make_job(fake_repo)
    fake_repo.enqueue_application(CANDIDATE, job["id"])

    other_job = _make_job(fake_repo, dice_job_id="DICE-OTHER")
    fake_repo.enqueue_application(OTHER_CANDIDATE, other_job["id"])

    fake_repo.claim_next_queued_application(CANDIDATE, "worker-1")
    other_claim = fake_repo.claim_next_queued_application(OTHER_CANDIDATE, "worker-1")

    assert other_claim is not None
    assert other_claim["candidate_id"] == OTHER_CANDIDATE


def test_requeue_failed_application_returns_to_queued(fake_repo):
    job = _make_job(fake_repo)
    app = fake_repo.enqueue_application(CANDIDATE, job["id"])
    fake_repo.update_application_status(app["id"], "PROCESSING")
    fake_repo.update_application_status(app["id"], "FAILED_RETRYABLE")

    requeued = fake_repo.requeue_failed_application(app["id"])

    assert requeued["status"] == "QUEUED"
    assert requeued["attempt_count"] == 1
    assert requeued["worker_id"] is None

    # And it's claimable again afterwards.
    claimed = fake_repo.claim_next_queued_application(CANDIDATE, "worker-2")
    assert claimed is not None
    assert claimed["id"] == app["id"]


def test_requeue_rejects_non_retryable_application(fake_repo):
    job = _make_job(fake_repo)
    app = fake_repo.enqueue_application(CANDIDATE, job["id"])  # still QUEUED

    try:
        fake_repo.requeue_failed_application(app["id"])
        assert False, "expected InvalidStatusTransitionError"
    except InvalidStatusTransitionError:
        pass


def test_event_creation(fake_repo):
    job = _make_job(fake_repo)
    app = fake_repo.enqueue_application(CANDIDATE, job["id"])

    event = fake_repo.add_event(app["id"], "worker_claimed", step="OPEN_JOB")

    assert event["application_id"] == app["id"]
    assert event["event_type"] == "worker_claimed"
