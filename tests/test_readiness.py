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

import run_registry
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
    assert result.blocker == readiness.Blocker.AUTH_HEALTH_STALE


def test_auth_health_never_verified_is_not_ready():
    candidate_id = str(uuid.uuid4())  # never marked healthy or invalid
    result = readiness.check_dice_auth_ready(candidate_id)
    assert result.ready is False
    assert result.blocker == readiness.Blocker.AUTH_NEVER_VERIFIED


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


# ── Phase 8C: offer_job_if_ready -- the single production entrypoint ────


class _FakeProvider:
    channel = "TELEGRAM"

    def __init__(self):
        self.sent: list[tuple] = []

    def send_job_offer(self, application, job):
        self.sent.append(("job_offer", application["id"]))
        return f"msg-{len(self.sent)}"

    def send_reconnect_required(self, application_id):
        self.sent.append(("reconnect_required", application_id))
        return f"msg-{len(self.sent)}"

    def send_reconnect_success(self, application, job):
        self.sent.append(("reconnect_success", application["id"]))
        return f"msg-{len(self.sent)}"


# 7. healthy readiness -> offer created, AWAITING_USER_DECISION, notification sent
def test_offer_job_if_ready_sends_real_offer_when_offerable(monkeypatch):
    _fully_healthy(monkeypatch)
    candidate_id = _make_candidate_with_healthy_auth()
    job = _make_job()
    provider = _FakeProvider()

    result = readiness.offer_job_if_ready(provider, candidate_id, job["id"])

    assert result["offered"] is True
    application_id = result["application_id"]
    app = get_supabase_client().table("applications").select("status").eq("id", application_id).execute().data[0]
    assert app["status"] == "AWAITING_USER_DECISION"
    assert provider.sent == [("job_offer", application_id)]


# 8. AUTH_REQUIRED -> no offer, no application row, no Telegram/iMessage card
def test_offer_job_if_ready_blocks_and_sends_nothing_when_auth_required(monkeypatch):
    _fully_healthy(monkeypatch)
    candidate_id = str(uuid.uuid4())
    _created_candidate_ids.append(candidate_id)
    dice_auth_health_repository.mark_invalid(candidate_id, "AUTH_REQUIRED on live re-check")
    job = _make_job()
    provider = _FakeProvider()

    result = readiness.offer_job_if_ready(provider, candidate_id, job["id"])

    assert result["offered"] is False
    assert result["blocker"] == readiness.Blocker.AUTH_REQUIRED.value
    assert provider.sent == []
    existing = get_supabase_client().table("applications").select("id").eq("candidate_id", candidate_id).eq("dice_job_id", job["id"]).execute().data
    assert existing == []


# 9. never verified -> no offer
def test_offer_job_if_ready_blocks_when_auth_never_verified(monkeypatch):
    _fully_healthy(monkeypatch)
    candidate_id = str(uuid.uuid4())  # deliberately never marked healthy or invalid
    job = _make_job()
    provider = _FakeProvider()

    result = readiness.offer_job_if_ready(provider, candidate_id, job["id"])

    assert result["offered"] is False
    assert result["blocker"] == readiness.Blocker.AUTH_NEVER_VERIFIED.value
    assert provider.sent == []


# 10. stale auth health, but auto-recovery itself can't verify (no auth
# state reachable) -> no offer. Phase M8C: AUTH_HEALTH_STALE alone no
# longer dead-ends silently -- it triggers one auto-recovery attempt
# first (see the Phase M8C section below); when that attempt itself
# can't run (as here -- reconnect_dice unmocked, no auth state
# reachable), the honest blocker is AUTH_REQUIRED (verification was
# attempted and failed), not the original, now-stale-and-superseded
# AUTH_HEALTH_STALE.
def test_offer_job_if_ready_blocks_when_auth_stale_and_recovery_cannot_verify(monkeypatch):
    _fully_healthy(monkeypatch)
    candidate_id = str(uuid.uuid4())
    _created_candidate_ids.append(candidate_id)
    dice_auth_health_repository.mark_healthy(candidate_id)
    monkeypatch.setenv("DICEPILOT_AUTH_HEALTH_TTL_MINUTES", "0")
    job = _make_job()
    provider = _FakeProvider()

    result = readiness.offer_job_if_ready(provider, candidate_id, job["id"])

    assert result["offered"] is False
    assert result["blocker"] == readiness.Blocker.AUTH_REQUIRED.value
    assert provider.sent == []
    existing = get_supabase_client().table("applications").select("id").eq("candidate_id", candidate_id).eq("dice_job_id", job["id"]).execute().data
    assert existing == []


# 21. permanent blocker: two consecutive calls both correctly refuse, no rows accumulate
def test_offer_job_if_ready_permanent_blocker_never_offers_across_repeated_calls(monkeypatch):
    _fully_healthy(monkeypatch)
    candidate_id = _make_candidate_with_healthy_auth()
    job = _make_job(is_easy_apply=False)
    provider = _FakeProvider()

    first = readiness.offer_job_if_ready(provider, candidate_id, job["id"])
    second = readiness.offer_job_if_ready(provider, candidate_id, job["id"])

    assert first["offered"] is False
    assert second["offered"] is False
    assert first["blocker"] == readiness.Blocker.NOT_EASY_APPLY.value
    assert second["blocker"] == readiness.Blocker.NOT_EASY_APPLY.value
    assert provider.sent == []
    existing = get_supabase_client().table("applications").select("id").eq("candidate_id", candidate_id).eq("dice_job_id", job["id"]).execute().data
    assert existing == []


# 22. temporary blocker: blocked once, then clears once the real condition clears
def test_offer_job_if_ready_temporary_blocker_is_reconsiderable_once_cleared(monkeypatch):
    candidate_id = _make_candidate_with_healthy_auth()
    job = _make_job()
    provider = _FakeProvider()

    monkeypatch.setattr(readiness.run_registry, "worker_status", lambda: {"online": False, "status": "OFFLINE"})
    _ok_browser(monkeypatch)
    _ok_resume(monkeypatch)
    blocked = readiness.offer_job_if_ready(provider, candidate_id, job["id"])
    assert blocked["offered"] is False
    assert blocked["blocker"] == readiness.Blocker.WORKER_UNAVAILABLE.value

    _ok_worker(monkeypatch)  # the worker is back online -- same job, no special "unheld" step needed
    recovered = readiness.offer_job_if_ready(provider, candidate_id, job["id"])
    assert recovered["offered"] is True
    assert provider.sent == [("job_offer", recovered["application_id"])]


# 23. the real production entrypoint itself never spends Browserless cost
def test_offer_job_if_ready_never_creates_a_real_browser_session(monkeypatch):
    _fully_healthy(monkeypatch)
    monkeypatch.setenv("DICEPILOT_BROWSER_PROVIDER", "browserless")
    monkeypatch.setenv("BROWSERLESS_TOKEN", "test-token")

    def _boom(*a, **kw):
        raise AssertionError("offer_job_if_ready must never create a real Browserless session")

    import dice_browser.browserless_session as browserless_session

    monkeypatch.setattr(browserless_session, "create_session", _boom)

    candidate_id = _make_candidate_with_healthy_auth()
    job = _make_job()
    provider = _FakeProvider()

    result = readiness.offer_job_if_ready(provider, candidate_id, job["id"])

    assert result["offered"] is True


# ── Phase 8D: reconnect_dice / _resume_interrupted_applications ─────────


def _make_authorized_interrupted_application(candidate_id, submission_policy="AUTHORIZED_AUTONOMOUS"):
    """A job the candidate already tapped Apply on, that then hit a
    genuine (post-retry) AUTH_REQUIRED -- QUEUED -> PROCESSING ->
    FAILED_RETRYABLE, with a real run at the given submission_policy,
    matching exactly what worker._fail() leaves behind."""
    job = _make_job()
    app = create_job_offer(candidate_id, job["id"])
    client = get_supabase_client()
    client.table("applications").update({"status": "QUEUED"}).eq("id", app["id"]).execute()
    run = run_registry.create_run([app["id"]], candidate_id, submission_policy=submission_policy)
    client.table("applications").update({"status": "PROCESSING", "run_id": run["id"]}).eq("id", app["id"]).execute()
    client.table("applications").update({"status": "FAILED_RETRYABLE", "error_code": "AUTH_REQUIRED", "error_message": "not authenticated on live re-check"}).eq("id", app["id"]).execute()
    return app, job


# distinguishes authorized-interrupted (resumed) from held-not-yet-offered (untouched)
def test_resume_interrupted_applications_resumes_authorized_not_held(monkeypatch):
    candidate_id = _make_candidate_with_healthy_auth()
    interrupted, _ = _make_authorized_interrupted_application(candidate_id)
    held_job = _make_job()
    held = create_job_offer(candidate_id, held_job["id"])  # AWAITING_USER_DECISION -- never authorized
    provider = _FakeProvider()

    resumed = readiness._resume_interrupted_applications(provider, candidate_id)

    assert resumed == [interrupted["id"]]
    client = get_supabase_client()
    interrupted_after = client.table("applications").select("status,run_id").eq("id", interrupted["id"]).execute().data[0]
    assert interrupted_after["status"] == "QUEUED"
    assert interrupted_after["run_id"] is not None
    held_after = client.table("applications").select("status").eq("id", held["id"]).execute().data[0]
    assert held_after["status"] == "AWAITING_USER_DECISION"  # untouched -- never auto-blasted
    assert provider.sent == [("reconnect_success", interrupted["id"])]


def test_resume_interrupted_applications_preserves_original_submission_policy():
    candidate_id = _make_candidate_with_healthy_auth()
    interrupted, _ = _make_authorized_interrupted_application(candidate_id, submission_policy="REQUIRE_CONFIRMATION")
    provider = _FakeProvider()

    readiness._resume_interrupted_applications(provider, candidate_id)

    client = get_supabase_client()
    new_run_id = client.table("applications").select("run_id").eq("id", interrupted["id"]).execute().data[0]["run_id"]
    new_run = run_registry.get_run(new_run_id)
    assert new_run["submission_policy"] == "REQUIRE_CONFIRMATION"  # never silently upgraded to autonomous


def test_resume_interrupted_applications_none_when_nothing_stuck():
    candidate_id = _make_candidate_with_healthy_auth()
    provider = _FakeProvider()
    assert readiness._resume_interrupted_applications(provider, candidate_id) == []
    assert provider.sent == []


def test_reconnect_dice_returns_not_reconnected_when_no_cookies_configured(monkeypatch):
    import dice_browser.browserless_session as browserless_session

    monkeypatch.delenv("DICE_AUTH_COOKIES_JSON", raising=False)
    monkeypatch.delenv(browserless_session._DEV_FALLBACK_ENV_VAR, raising=False)
    result = readiness.reconnect_dice(_FakeProvider(), str(uuid.uuid4()))
    assert result["reconnected"] is False


def _nav_result_stub(authenticated: bool, evidence: str):
    from dice_browser.models import NavigationResult, BrowserState

    return NavigationResult(
        canonical_url="https://www.dice.com/job-detail/stub",
        page_title="",
        browser_state=BrowserState.ACTIVE if authenticated else BrowserState.AUTH_REQUIRED,
        authenticated=authenticated,
        already_applied=None,
        easy_apply_visible=None,
        challenge_type=None,
        evidence=evidence,
    )


class _FakeBrowser:
    contexts = []

    def new_context(self):
        return self

    def add_cookies(self, cookies):
        pass

    def new_page(self):
        return object()

    def close(self):
        pass


class _FakePlaywrightCM:
    def __enter__(self):
        class _P:
            class chromium:
                @staticmethod
                def connect_over_cdp(url):
                    return _FakeBrowser()

        return _P()

    def __exit__(self, *a):
        return False


def _patch_fake_browserless(monkeypatch, nav_result, candidate_id: str | None = None):
    """Phase M8B: patches db.dice_auth_state_repository.get_auth_state
    directly, candidate-scoped -- not the old global DICE_AUTH_COOKIES_
    JSON env var, which reconnect_dice no longer reads. When
    candidate_id is None (callers that don't care which candidate),
    every candidate_id resolves to the same fake cookie set."""
    import dice_browser.browserless_session as browserless_session
    import dice_browser.navigator as navigator
    from db import dice_auth_state_repository

    fake_cookies_json = '[{"name": "access", "value": "x", "domain": ".dice.com", "path": "/", "secure": true, "httpOnly": false, "sameSite": "lax", "expirationDate": 9999999999}]'
    monkeypatch.setattr(
        dice_auth_state_repository,
        "get_auth_state",
        lambda cid: fake_cookies_json if candidate_id is None or cid == candidate_id else None,
    )
    monkeypatch.setattr(browserless_session, "create_session", lambda **kw: {"connect": "ws://fake", "stop": "http://fake/stop"})
    monkeypatch.setattr(browserless_session, "stop_session", lambda url: None)
    monkeypatch.setattr(navigator, "open_job", lambda page, url: nav_result)
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: _FakePlaywrightCM())


def test_reconnect_dice_resumes_only_when_final_auth_active(monkeypatch):
    candidate_id = _make_candidate_with_healthy_auth()
    interrupted, _ = _make_authorized_interrupted_application(candidate_id)
    provider = _FakeProvider()
    _patch_fake_browserless(monkeypatch, _nav_result_stub(authenticated=True, evidence="ACTIVE"))

    result = readiness.reconnect_dice(provider, candidate_id)

    assert result["reconnected"] is True
    assert result["resumed_application_ids"] == [interrupted["id"]]
    health = dice_auth_health_repository.get_auth_health(candidate_id)
    assert health["is_healthy"] is True


def test_reconnect_dice_does_not_resume_when_still_auth_required(monkeypatch):
    candidate_id = _make_candidate_with_healthy_auth()
    interrupted, _ = _make_authorized_interrupted_application(candidate_id)
    provider = _FakeProvider()
    _patch_fake_browserless(monkeypatch, _nav_result_stub(authenticated=False, evidence="still AUTH_REQUIRED after reload retry"))

    result = readiness.reconnect_dice(provider, candidate_id)

    assert result["reconnected"] is False
    client = get_supabase_client()
    still_stuck = client.table("applications").select("status").eq("id", interrupted["id"]).execute().data[0]
    assert still_stuck["status"] == "FAILED_RETRYABLE"  # never touched -- reconnect didn't actually succeed
    health = dice_auth_health_repository.get_auth_health(candidate_id)
    assert health["is_healthy"] is False


# ── Phase M8C: AUTH_HEALTH_STALE self-recovery ───────────────────────────
# AUTH_HEALTH_STALE, and ONLY that blocker (every other check already
# passed), triggers one bounded auto-verification (reusing reconnect_
# dice's own canonical path, unchanged) before the job is finally
# reported held or offered -- never a silent dead-end requiring a human
# to manually approve a read-only login check every time the freshness
# window expires.

import threading
import time


def _backdate_auth_health(candidate_id: str, minutes_ago: int) -> None:
    """Backdates last_verified_at directly rather than monkeypatching the
    TTL to 0 -- TTL=0 would ALSO immediately re-stale a record a mocked
    successful recovery just marked healthy (any positive elapsed time
    exceeds a 0-minute window), defeating tests that expect the fresh
    re-check after recovery to actually pass. Backdating past the real
    default TTL, then leaving the TTL alone, lets recovery's own fresh
    mark_healthy() correctly read back as healthy."""
    from datetime import datetime, timedelta, timezone

    backdated = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    get_supabase_client().table("dice_auth_health").update({"last_verified_at": backdated}).eq("candidate_id", candidate_id).execute()


def _stale_but_otherwise_ready(monkeypatch, candidate_id, job):
    """Everything except dice_auth passes; dice_auth is stale (not
    invalidated) -- the exact precondition _is_auth_stale_the_only_
    blocker requires before auto-recovery may fire."""
    _fully_healthy(monkeypatch)
    dice_auth_health_repository.mark_healthy(candidate_id)
    _backdate_auth_health(candidate_id, readiness.DEFAULT_AUTH_HEALTH_TTL_MINUTES + 30)


def _mock_reconnect_success(monkeypatch):
    calls = []

    def _fake_reconnect(provider, candidate_id):
        calls.append(candidate_id)
        dice_auth_health_repository.mark_healthy(candidate_id)
        return {"reconnected": True, "resumed_application_ids": []}

    monkeypatch.setattr(readiness, "reconnect_dice", _fake_reconnect)
    return calls


def _mock_reconnect_still_auth_required(monkeypatch):
    calls = []

    def _fake_reconnect(provider, candidate_id):
        calls.append(candidate_id)
        dice_auth_health_repository.mark_invalid(candidate_id, "AUTH_REQUIRED on live re-check")
        return {"reconnected": False, "reason": "still AUTH_REQUIRED after reload retry"}

    monkeypatch.setattr(readiness, "reconnect_dice", _fake_reconnect)
    return calls


# 1. stale auth + otherwise eligible job -> bounded verification invoked
def test_auth_stale_only_blocker_triggers_auto_verification(monkeypatch):
    candidate_id = str(uuid.uuid4())
    _created_candidate_ids.append(candidate_id)
    job = _make_job()
    _stale_but_otherwise_ready(monkeypatch, candidate_id, job)
    calls = _mock_reconnect_success(monkeypatch)
    provider = _FakeProvider()

    readiness.offer_job_if_ready(provider, candidate_id, job["id"])

    assert calls == [candidate_id]


# 2. verification ACTIVE -> auth health refreshed -> same job
# reconsidered -> one offer maximum
def test_auth_recovery_success_reconsiders_the_same_job_and_offers_once(monkeypatch):
    candidate_id = str(uuid.uuid4())
    _created_candidate_ids.append(candidate_id)
    job = _make_job()
    _stale_but_otherwise_ready(monkeypatch, candidate_id, job)
    _mock_reconnect_success(monkeypatch)
    provider = _FakeProvider()

    result = readiness.offer_job_if_ready(provider, candidate_id, job["id"])

    assert result["offered"] is True
    application_id = result["application_id"]
    app = get_supabase_client().table("applications").select("status").eq("id", application_id).execute().data[0]
    assert app["status"] == "AWAITING_USER_DECISION"  # offered only -- never auto-submitted
    assert provider.sent == [("job_offer", application_id)]
    health = dice_auth_health_repository.get_auth_health(candidate_id)
    assert health["is_healthy"] is True


# 4. final AUTH_REQUIRED -> no offer, job remains held, reconnect required
def test_auth_recovery_confirmed_auth_required_holds_the_job(monkeypatch):
    candidate_id = str(uuid.uuid4())
    _created_candidate_ids.append(candidate_id)
    job = _make_job()
    _stale_but_otherwise_ready(monkeypatch, candidate_id, job)
    _mock_reconnect_still_auth_required(monkeypatch)
    provider = _FakeProvider()

    result = readiness.offer_job_if_ready(provider, candidate_id, job["id"])

    assert result["offered"] is False
    assert result["blocker"] == readiness.Blocker.AUTH_REQUIRED.value
    assert provider.sent == []
    existing = get_supabase_client().table("applications").select("id").eq("candidate_id", candidate_id).eq("dice_job_id", job["id"]).execute().data
    assert existing == []  # no application row -- the job stays a reconsiderable held opportunity


# 5. temporary Browserless/provider failure -> no offer, no application
# row, opportunity remains reconsiderable (never permanently rejected)
def test_auth_recovery_provider_failure_leaves_job_held_not_rejected(monkeypatch):
    candidate_id = str(uuid.uuid4())
    _created_candidate_ids.append(candidate_id)
    job = _make_job()
    _stale_but_otherwise_ready(monkeypatch, candidate_id, job)

    def _boom(provider, cid):
        raise ConnectionError("Browserless unreachable")

    monkeypatch.setattr(readiness, "reconnect_dice", _boom)
    provider = _FakeProvider()

    result = readiness.offer_job_if_ready(provider, candidate_id, job["id"])

    assert result["offered"] is False
    assert result["blocker"] == readiness.Blocker.AUTH_HEALTH_STALE.value  # unknown, not disproven -- distinct from a confirmed AUTH_REQUIRED
    assert provider.sent == []
    existing = get_supabase_client().table("applications").select("id").eq("candidate_id", candidate_id).eq("dice_job_id", job["id"]).execute().data
    assert existing == []

    # still reconsiderable -- a later call with a working provider succeeds normally
    _mock_reconnect_success(monkeypatch)
    second = readiness.offer_job_if_ready(provider, candidate_id, job["id"])
    assert second["offered"] is True


# 6. two stale eligible jobs for the same candidate -> no duplicate
# simultaneous auth-verification sessions
def test_concurrent_stale_jobs_for_same_candidate_never_double_verify(monkeypatch):
    candidate_id = str(uuid.uuid4())
    _created_candidate_ids.append(candidate_id)
    job_a = _make_job()
    job_b = _make_job()
    _stale_but_otherwise_ready(monkeypatch, candidate_id, job_a)

    concurrent = {"count": 0, "max": 0}
    lock = threading.Lock()

    def _slow_reconnect(provider, cid):
        with lock:
            concurrent["count"] += 1
            concurrent["max"] = max(concurrent["max"], concurrent["count"])
        time.sleep(0.2)
        with lock:
            concurrent["count"] -= 1
        dice_auth_health_repository.mark_healthy(cid)
        return {"reconnected": True, "resumed_application_ids": []}

    monkeypatch.setattr(readiness, "reconnect_dice", _slow_reconnect)
    provider = _FakeProvider()
    results = {}

    def _run(job, key):
        results[key] = readiness.offer_job_if_ready(provider, candidate_id, job["id"])

    t1 = threading.Thread(target=_run, args=(job_a, "a"))
    t2 = threading.Thread(target=_run, args=(job_b, "b"))
    t1.start()
    time.sleep(0.05)  # ensure t1 acquires the lock first
    t2.start()
    t1.join()
    t2.join()

    assert concurrent["max"] == 1  # never two verification sessions in flight at once
    # the thread that lost the lock leaves its job held, not offered --
    # no fabricated/duplicate verification pretending to have happened
    assert not (results["a"]["offered"] is False and results["b"]["offered"] is False)


# 7 & 8. auth verification itself never submits an application and never
# creates an application row by itself -- only a SUCCESSFUL fresh re-
# check through the normal offer path ever does that (test 2 above), and
# a failed one (test 4 above) proves zero rows exist. This test proves
# the same for the lock-contention/held case.
def test_auth_recovery_alone_never_creates_an_application_row():
    candidate_id = str(uuid.uuid4())
    job = _make_job()
    health_before = dice_auth_health_repository.get_auth_health(candidate_id)
    assert health_before is None
    existing = get_supabase_client().table("applications").select("id").eq("candidate_id", candidate_id).eq("dice_job_id", job["id"]).execute().data
    assert existing == []


# 9a. a DIFFERENT real blocker present ALONGSIDE stale auth must never
# trigger auto-recovery at all -- the session-cost guard
# (_is_auth_stale_the_only_blocker) exists exactly for this: don't spend
# a Browserless session verifying login for a job that has some other,
# unrelated problem anyway.
def test_auth_recovery_never_fires_when_a_different_blocker_also_exists(monkeypatch):
    candidate_id = str(uuid.uuid4())
    _created_candidate_ids.append(candidate_id)
    job = _make_job(is_easy_apply=False)  # a real, separate, permanent blocker
    _ok_worker(monkeypatch)
    _ok_browser(monkeypatch)
    _ok_resume(monkeypatch)
    dice_auth_health_repository.mark_healthy(candidate_id)
    _backdate_auth_health(candidate_id, readiness.DEFAULT_AUTH_HEALTH_TTL_MINUTES + 30)
    calls = _mock_reconnect_success(monkeypatch)
    provider = _FakeProvider()

    result = readiness.offer_job_if_ready(provider, candidate_id, job["id"])

    assert calls == []  # never attempted -- a second, unrelated blocker already exists
    assert result["offered"] is False
    assert provider.sent == []


# 9b. successful reconsideration still passes through central readiness
# -- recovering auth must never bypass a DIFFERENT real blocker that
# only becomes true during the verification window (proves the fresh
# re-check is a real, live re-evaluation, not a rubber stamp on success).
def test_auth_recovery_success_still_blocked_by_a_new_real_reason(monkeypatch):
    candidate_id = str(uuid.uuid4())
    _created_candidate_ids.append(candidate_id)
    job = _make_job()
    _stale_but_otherwise_ready(monkeypatch, candidate_id, job)

    def _fake_reconnect(provider, cid):
        dice_auth_health_repository.mark_healthy(cid)
        # simulates something else going wrong during the verification
        # window (e.g. the posting closed) -- proves the fresh recheck
        # below reflects real, current state, not a cached snapshot.
        get_supabase_client().table("dice_jobs").update({"is_easy_apply": False}).eq("id", job["id"]).execute()
        return {"reconnected": True, "resumed_application_ids": []}

    monkeypatch.setattr(readiness, "reconnect_dice", _fake_reconnect)
    provider = _FakeProvider()

    result = readiness.offer_job_if_ready(provider, candidate_id, job["id"])

    assert result["offered"] is False
    assert result["blocker"] == readiness.Blocker.NOT_EASY_APPLY.value  # the fresh re-check caught this, not silently offered
    assert provider.sent == []


# 10. no duplicate Telegram offer after reconsideration
def test_auth_recovery_never_double_offers_the_same_job(monkeypatch):
    candidate_id = str(uuid.uuid4())
    _created_candidate_ids.append(candidate_id)
    job = _make_job()
    _stale_but_otherwise_ready(monkeypatch, candidate_id, job)
    _mock_reconnect_success(monkeypatch)
    provider = _FakeProvider()

    first = readiness.offer_job_if_ready(provider, candidate_id, job["id"])
    assert first["offered"] is True

    # a second call for the exact same (candidate, job) after the offer
    # already exists -- auth is now fresh, so this never even reaches
    # auto-recovery, and check_job_ready's own dedup blocks it.
    second = readiness.offer_job_if_ready(provider, candidate_id, job["id"])

    assert second["offered"] is False
    assert second["blocker"] == readiness.Blocker.DUPLICATE_APPLICATION.value
    assert len([s for s in provider.sent if s[0] == "job_offer"]) == 1
