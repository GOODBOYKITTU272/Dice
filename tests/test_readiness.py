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


# 10. stale auth health -> no offer
def test_offer_job_if_ready_blocks_when_auth_stale(monkeypatch):
    _fully_healthy(monkeypatch)
    candidate_id = str(uuid.uuid4())
    _created_candidate_ids.append(candidate_id)
    dice_auth_health_repository.mark_healthy(candidate_id)
    monkeypatch.setenv("DICEPILOT_AUTH_HEALTH_TTL_MINUTES", "0")
    job = _make_job()
    provider = _FakeProvider()

    result = readiness.offer_job_if_ready(provider, candidate_id, job["id"])

    assert result["offered"] is False
    assert result["blocker"] == readiness.Blocker.AUTH_HEALTH_STALE.value
    assert provider.sent == []


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
    monkeypatch.delenv("DICE_AUTH_COOKIES_JSON", raising=False)
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


def _patch_fake_browserless(monkeypatch, nav_result):
    import dice_browser.browserless_session as browserless_session
    import dice_browser.navigator as navigator

    monkeypatch.setenv("DICE_AUTH_COOKIES_JSON", '[{"name": "access", "value": "x", "domain": ".dice.com", "path": "/", "secure": true, "httpOnly": false, "sameSite": "lax", "expirationDate": 9999999999}]')
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
