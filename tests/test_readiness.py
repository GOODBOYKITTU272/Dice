"""Phase 8A: readiness.py -- the pre-offer gate. Worker/browser-provider
checks are monkeypatched (their own real logic is already covered by
run_registry's and browserless_session's own tests); dice_auth_health
and job/application eligibility run against real Supabase, since dedup
correctness is exactly the kind of thing a fake client can't be trusted
to prove.
"""
from __future__ import annotations

import uuid

import pytest

import readiness
from db import dice_auth_health_repository
from db.application_repository import create_job_offer, upsert_dice_job
from db.supabase_client import get_supabase_client

_created_job_ids: list[str] = []
_created_candidate_ids: list[str] = []


@pytest.fixture(autouse=True)
def _cleanup(live_client):
    yield
    client = get_supabase_client()
    while _created_job_ids:
        job_id = _created_job_ids.pop()
        for a in client.table("applications").select("id").eq("dice_job_id", job_id).execute().data:
            client.table("applications").delete().eq("id", a["id"]).execute()
        client.table("dice_jobs").delete().eq("id", job_id).execute()
    while _created_candidate_ids:
        client.table("dice_auth_health").delete().eq("candidate_id", _created_candidate_ids.pop()).execute()


def _make_job(dice_job_id=None, c2c_status="LIKELY", is_easy_apply=True):
    # Deliberately NOT prefixed "TEST-"/"SYNTHETIC-" -- that prefix is
    # exactly what check_job_ready() rejects as a leftover artifact, and
    # these are real, valid-in-context fixture jobs the readiness gate
    # should evaluate normally (cleaned up by the _cleanup fixture).
    dice_job_id = dice_job_id or f"READINESS-CHECK-{uuid.uuid4()}"
    job = upsert_dice_job(
        {
            "dice_job_id": dice_job_id,
            "canonical_url": f"https://dice.com/job-detail/{dice_job_id}",
            "title": "Readiness Test Role",
            "company_name": "Test Co",
            "c2c_status": c2c_status,
            "is_easy_apply": is_easy_apply,
        }
    )
    _created_job_ids.append(job["id"])
    return job


def _make_candidate_with_healthy_auth():
    candidate_id = str(uuid.uuid4())
    dice_auth_health_repository.mark_healthy(candidate_id)
    _created_candidate_ids.append(candidate_id)
    return candidate_id


def _ok_worker(monkeypatch):
    monkeypatch.setattr(readiness.run_registry, "worker_status", lambda: {"online": True, "status": "ONLINE"})


def _ok_browser(monkeypatch):
    monkeypatch.setenv("DICEPILOT_BROWSER_PROVIDER", "local")


def _ok_resume(monkeypatch):
    monkeypatch.setattr(readiness, "resume_exists_in_storage", lambda candidate_id: True)


def _fully_healthy(monkeypatch):
    _ok_worker(monkeypatch)
    _ok_browser(monkeypatch)
    _ok_resume(monkeypatch)


# 1. everything healthy -> offerable
def test_offerable_when_everything_healthy(monkeypatch):
    _fully_healthy(monkeypatch)
    candidate_id = _make_candidate_with_healthy_auth()
    job = _make_job()

    result = readiness.evaluate_offer_readiness(candidate_id, job["id"])

    assert result.offerable is True
    assert result.blocker is None


# 2. Dice AUTH_REQUIRED -> not offerable
def test_not_offerable_when_auth_invalid(monkeypatch):
    _fully_healthy(monkeypatch)
    candidate_id = str(uuid.uuid4())
    _created_candidate_ids.append(candidate_id)
    dice_auth_health_repository.mark_invalid(candidate_id, "AUTH_REQUIRED on live re-check")
    job = _make_job()

    result = readiness.evaluate_offer_readiness(candidate_id, job["id"])

    assert result.offerable is False
    assert result.blocker == readiness.Blocker.AUTH_REQUIRED
    assert result.dice_auth.ready is False


# 3. worker offline -> not offerable
def test_not_offerable_when_worker_offline(monkeypatch):
    monkeypatch.setattr(readiness.run_registry, "worker_status", lambda: {"online": False, "status": "OFFLINE"})
    _ok_browser(monkeypatch)
    _ok_resume(monkeypatch)
    candidate_id = _make_candidate_with_healthy_auth()
    job = _make_job()

    result = readiness.evaluate_offer_readiness(candidate_id, job["id"])

    assert result.offerable is False
    assert result.blocker == readiness.Blocker.WORKER_UNAVAILABLE


# 4. Browserless not configured -> not offerable
def test_not_offerable_when_browserless_unconfigured(monkeypatch):
    _ok_worker(monkeypatch)
    _ok_resume(monkeypatch)
    monkeypatch.setenv("DICEPILOT_BROWSER_PROVIDER", "browserless")
    monkeypatch.delenv("BROWSERLESS_TOKEN", raising=False)
    candidate_id = _make_candidate_with_healthy_auth()
    job = _make_job()

    result = readiness.evaluate_offer_readiness(candidate_id, job["id"])

    assert result.offerable is False
    assert result.blocker == readiness.Blocker.BROWSER_UNAVAILABLE


# 5. resume missing -> not offerable
def test_not_offerable_when_resume_missing(monkeypatch):
    _ok_worker(monkeypatch)
    _ok_browser(monkeypatch)
    monkeypatch.setattr(readiness, "resume_exists_in_storage", lambda candidate_id: False)
    candidate_id = _make_candidate_with_healthy_auth()
    job = _make_job()

    result = readiness.evaluate_offer_readiness(candidate_id, job["id"])

    assert result.offerable is False
    assert result.blocker == readiness.Blocker.RESUME_MISSING


# 6. job already submitted -> not offerable
def test_not_offerable_when_already_applied(monkeypatch):
    _fully_healthy(monkeypatch)
    candidate_id = _make_candidate_with_healthy_auth()
    job = _make_job()
    app = create_job_offer(candidate_id, job["id"])
    get_supabase_client().table("applications").update({"status": "SUBMITTED"}).eq("id", app["id"]).execute()

    result = readiness.evaluate_offer_readiness(candidate_id, job["id"])

    assert result.offerable is False
    assert result.blocker == readiness.Blocker.ALREADY_APPLIED


# 7. duplicate active application -> not offerable
def test_not_offerable_when_application_already_active(monkeypatch):
    _fully_healthy(monkeypatch)
    candidate_id = _make_candidate_with_healthy_auth()
    job = _make_job()
    create_job_offer(candidate_id, job["id"])  # AWAITING_USER_DECISION -- already active

    result = readiness.evaluate_offer_readiness(candidate_id, job["id"])

    assert result.offerable is False
    assert result.blocker == readiness.Blocker.DUPLICATE_APPLICATION


# 8. job not Easy Apply -> not offerable
def test_not_offerable_when_not_easy_apply(monkeypatch):
    _fully_healthy(monkeypatch)
    candidate_id = _make_candidate_with_healthy_auth()
    job = _make_job(is_easy_apply=False)

    result = readiness.evaluate_offer_readiness(candidate_id, job["id"])

    assert result.offerable is False
    assert result.blocker == readiness.Blocker.NOT_EASY_APPLY


# 9. non-C2C job -> not offerable
def test_not_offerable_when_not_c2c(monkeypatch):
    _fully_healthy(monkeypatch)
    candidate_id = _make_candidate_with_healthy_auth()
    job = _make_job(c2c_status="UNKNOWN")

    result = readiness.evaluate_offer_readiness(candidate_id, job["id"])

    assert result.offerable is False
    assert result.blocker == readiness.Blocker.NOT_ELIGIBLE


def test_synthetic_job_never_offerable(monkeypatch):
    _fully_healthy(monkeypatch)
    candidate_id = _make_candidate_with_healthy_auth()
    job = _make_job(dice_job_id=f"SYNTHETIC-{uuid.uuid4()}")

    result = readiness.evaluate_offer_readiness(candidate_id, job["id"])

    assert result.offerable is False
    assert result.blocker == readiness.Blocker.NOT_ELIGIBLE


# 16. a known AUTH_REQUIRED must never be treated as healthy, even
# immediately after (well within any TTL window).
def test_invalidated_auth_never_cached_as_healthy_even_when_fresh():
    candidate_id = str(uuid.uuid4())
    _created_candidate_ids.append(candidate_id)
    dice_auth_health_repository.mark_healthy(candidate_id)
    dice_auth_health_repository.mark_invalid(candidate_id, "AUTH_REQUIRED on live re-check")

    result = readiness.check_dice_auth_ready(candidate_id)

    assert result.ready is False
    assert result.blocker == readiness.Blocker.AUTH_REQUIRED


def test_auth_health_stale_beyond_ttl_is_not_ready(monkeypatch):
    candidate_id = str(uuid.uuid4())
    _created_candidate_ids.append(candidate_id)
    dice_auth_health_repository.mark_healthy(candidate_id)
    monkeypatch.setenv("DICEPILOT_AUTH_HEALTH_TTL_MINUTES", "0")

    result = readiness.check_dice_auth_ready(candidate_id)

    assert result.ready is False


def test_auth_health_never_verified_is_not_ready():
    candidate_id = str(uuid.uuid4())  # never marked healthy or invalid
    result = readiness.check_dice_auth_ready(candidate_id)
    assert result.ready is False
    assert result.blocker == readiness.Blocker.AUTH_REQUIRED


# 17. the readiness gate itself must never spend real Browserless cost
# -- it's a config-presence check only, never a real session create.
def test_browser_provider_check_never_creates_a_real_session(monkeypatch):
    monkeypatch.setenv("DICEPILOT_BROWSER_PROVIDER", "browserless")
    monkeypatch.setenv("BROWSERLESS_TOKEN", "test-token")

    def _boom(*a, **kw):
        raise AssertionError("readiness must never create a real Browserless session")

    import dice_browser.browserless_session as browserless_session

    monkeypatch.setattr(browserless_session, "create_session", _boom)

    result = readiness.check_browser_provider_ready()

    assert result.ready is True
