"""Phase 7.3: automatic Steel session recovery. Covers the 18 explicit
requirements from the "make Steel session recovery fully automatic" task:

 1. Steel API healthy + no session -> auto-create
 2. existing live session is reused, not recreated
 3. dead/non-live session is detected (not treated as usable)
 4. a fresh session is created after a dead session
 5. a CDP disconnect triggers bounded recovery
 6. a fresh session/reconnect reuses the persistent profile (existing
    coverage: tests/test_dice_browser_session.py's Singleton-lock suite;
    _connect_for_provider's steel branch always clears locks against
    DICEPILOT_BROWSER_PROFILE_DIR, the same durable path every time --
    exercised directly here too)
 7. Dice auth is rechecked after a fresh connect
 8. an authenticated reconnect resumes work safely (existing coverage:
    test_worker_daemon_architecture.py's claim/process test -- unaffected
    by the recovery path, since a healthy connect never diverts through it)
 9. AUTH_REQUIRED pauses without crashing (visible via heartbeat; per-run
    halting on this is existing, unchanged Phase 6 behavior --
    dice_browser.worker._SESSION_LEVEL_STOPS -- reused as-is)
10. SECURITY_CHALLENGE stops without crashing (same as 9)
11. viewer closure has no effect on worker lifecycle (structural: nothing
    in this codebase ever calls or depends on Steel's viewer UI --
    grepped, not exercised at runtime)
12. bounded retry behavior -- never infinite
13. no duplicate Submit after a disconnect (reconcile_run_after_disconnect
    never re-drives SUBMITTING back through Submit)
14. pre-submit (PROCESSING) disconnect recovers safely to QUEUED
15. post-submit-boundary (SUBMITTING) disconnect never blind-retries --
    lands on FAILED_RETRYABLE for human verification
16. Singleton cleanup is still safe (existing coverage, unaffected:
    tests/test_dice_browser_session.py)
17. local provider behavior is unchanged (no Steel calls at all)
18. no real Dice mutation in tests -- every Steel HTTP call and every
    Playwright/CDP connection is mocked throughout this file
"""
from __future__ import annotations

import uuid

import pytest
import requests

import dice_browser.worker_daemon as worker_daemon
import run_registry
from db.application_repository import enqueue_application, get_application, update_application_status, upsert_dice_job
from dice_browser.steel_session import SteelUnavailableError, ensure_steel_session, resolve_steel_base_url

CANDIDATE = "cccccccc-cccc-cccc-cccc-cccccccccccc"


def _make_job_and_application(title):
    dice_job_id = f"TEST-{uuid.uuid4()}"
    job = upsert_dice_job(
        {"dice_job_id": dice_job_id, "canonical_url": f"https://dice.com/job/{dice_job_id}", "title": title, "is_easy_apply": True}
    )
    application = enqueue_application(CANDIDATE, job["id"])
    return job, application


def _cleanup(*job_ids: str):
    from db.application_repository import get_supabase_client

    sc = get_supabase_client()
    all_run_ids: set[str] = set()
    for job_id in job_ids:
        apps = sc.table("applications").select("id, run_id").eq("dice_job_id", job_id).execute().data
        all_run_ids.update(a["run_id"] for a in apps if a.get("run_id"))
        for a in apps:
            sc.table("applications").delete().eq("id", a["id"]).execute()
        sc.table("dice_jobs").delete().eq("id", job_id).execute()
    for run_id in all_run_ids:
        sc.table("application_runs").delete().eq("id", run_id).execute()


class _FakeResponse:
    def __init__(self, payload, ok=True):
        self._payload = payload
        self.ok = ok

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError("boom")

    def json(self):
        return self._payload


# 1. Steel API healthy, no live session -> one is auto-created via POST.
def test_ensure_steel_session_creates_when_none_live(monkeypatch):
    calls = []

    def _fake_get(url, timeout):
        calls.append(("GET", url))
        return _FakeResponse({"sessions": []})

    def _fake_post(url, json, timeout):
        calls.append(("POST", url))
        return _FakeResponse({"id": "new-session", "status": "live"})

    monkeypatch.setattr(requests, "get", _fake_get)
    monkeypatch.setattr(requests, "post", _fake_post)

    session = ensure_steel_session("http://localhost:3000")
    assert session == {"id": "new-session", "status": "live"}
    assert ("POST", "http://localhost:3000/v1/sessions") in calls


# 2. An existing live session is reused -- no POST/create call made.
def test_ensure_steel_session_reuses_existing_live_session(monkeypatch):
    post_called = []
    monkeypatch.setattr(requests, "get", lambda url, timeout: _FakeResponse({"sessions": [{"id": "existing", "status": "live"}]}))
    monkeypatch.setattr(requests, "post", lambda *a, **kw: post_called.append(1))

    session = ensure_steel_session("http://localhost:3000")
    assert session == {"id": "existing", "status": "live"}
    assert post_called == []


# 3. A non-live (dead/released) session is detected and not treated as usable.
def test_ensure_steel_session_ignores_dead_session(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda url, timeout: _FakeResponse({"sessions": [{"id": "old", "status": "released"}]}))
    monkeypatch.setattr(requests, "post", lambda url, json, timeout: _FakeResponse({"id": "fresh", "status": "live"}))

    session = ensure_steel_session("http://localhost:3000")
    assert session["id"] == "fresh"


# 4. A fresh session is created specifically because the only session found was dead.
def test_ensure_steel_session_creates_fresh_after_dead_session(monkeypatch):
    posts = []
    monkeypatch.setattr(requests, "get", lambda url, timeout: _FakeResponse({"sessions": [{"id": "old", "status": "released"}]}))

    def _fake_post(url, json, timeout):
        posts.append(1)
        return _FakeResponse({"id": "fresh-2", "status": "live"})

    monkeypatch.setattr(requests, "post", _fake_post)
    session = ensure_steel_session("http://localhost:3000")
    assert session["id"] == "fresh-2"
    assert posts == [1]


def test_ensure_steel_session_raises_when_api_unreachable(monkeypatch):
    def _boom(url, timeout):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(requests, "get", _boom)
    with pytest.raises(SteelUnavailableError):
        ensure_steel_session("http://localhost:3000")


def test_resolve_steel_base_url_derives_from_ws_cdp_url(monkeypatch):
    monkeypatch.delenv("STEEL_BASE_URL", raising=False)
    assert resolve_steel_base_url("ws://localhost:3000/") == "http://localhost:3000/"


def test_resolve_steel_base_url_prefers_explicit_env(monkeypatch):
    monkeypatch.setenv("STEEL_BASE_URL", "http://steel.internal:3000")
    assert resolve_steel_base_url("ws://localhost:3000/") == "http://steel.internal:3000"


# Phase 2 (Railway deployment): the worker must never assume Steel always
# runs on port 3000, and must never treat Steel's own self-reported
# session URL (which can be a meaningless bind address like 0.0.0.0) as a
# real connection target. resolve_steel_base_url only ever derives from
# the OPERATOR-configured DICEPILOT_CDP_URL/STEEL_BASE_URL -- never from
# a session's own JSON -- so whatever port/host that config carries
# (Railway's 8080, a private *.railway.internal DNS name, or anything
# else) passes through untouched, with no hardcoded port anywhere.
def test_resolve_steel_base_url_preserves_railway_private_dns_and_port(monkeypatch):
    monkeypatch.delenv("STEEL_BASE_URL", raising=False)
    assert (
        resolve_steel_base_url("ws://steel-browser-api.railway.internal:8080/")
        == "http://steel-browser-api.railway.internal:8080/"
    )


def test_resolve_steel_base_url_preserves_url_with_no_port(monkeypatch):
    monkeypatch.delenv("STEEL_BASE_URL", raising=False)
    assert resolve_steel_base_url("ws://steel-browser-api.railway.internal/") == "http://steel-browser-api.railway.internal/"


def test_resolve_steel_base_url_passthrough_on_scheme_less_input(monkeypatch):
    monkeypatch.delenv("STEEL_BASE_URL", raising=False)
    # No ws:// / wss:// prefix to rewrite -- passed through unchanged
    # rather than guessed at. The network call this then feeds
    # (steel_api_healthy/ensure_steel_session) fails cleanly and reports
    # SteelUnavailableError; there's no separate validation layer to
    # maintain for a config error that already surfaces obviously.
    assert resolve_steel_base_url("not-a-url") == "not-a-url"


def test_ensure_steel_session_zero_bind_address_in_response_is_never_used_as_connect_target(monkeypatch):
    # Steel's self-hosted session JSON can report websocketUrl as
    # ws://0.0.0.0:8080/ -- a bind address, not a dialable host. Proves
    # the worker never reads that field: ensure_steel_session's return
    # value is discarded by every caller, and the real CDP connection
    # always uses the operator-configured cdp_url instead.
    monkeypatch.setattr(
        worker_daemon,
        "ensure_steel_session",
        lambda base_url: {"id": "x", "status": "live", "websocketUrl": "ws://0.0.0.0:8080/"},
    )
    monkeypatch.setattr(worker_daemon, "clean_stale_singleton_locks", lambda profile_dir: [])
    seen_cdp_urls = []
    monkeypatch.setattr(worker_daemon, "_connect", lambda cdp_url: seen_cdp_urls.append(cdp_url) or ("pw", "page"))

    worker_daemon._connect_for_provider("ws://steel-browser-api.railway.internal:8080/", "steel")
    assert seen_cdp_urls == ["ws://steel-browser-api.railway.internal:8080/"]


# 5 & 12. A CDP disconnect triggers recovery, bounded -- never infinite --
# and succeeds once the underlying problem clears up.
def test_connect_with_recovery_retries_bounded_then_succeeds(monkeypatch):
    attempts = []

    def _flaky_connect(cdp_url, provider):
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionError("not ready yet")
        return "playwright", "page"

    monkeypatch.setattr(worker_daemon, "_connect_for_provider", _flaky_connect)
    result = worker_daemon._connect_with_recovery("http://x", "local", max_attempts=5, backoff_seconds=0)
    assert result == ("playwright", "page")
    assert len(attempts) == 3


def test_connect_with_recovery_gives_up_after_max_attempts(monkeypatch):
    attempts = []

    def _always_fails(cdp_url, provider):
        attempts.append(1)
        raise ConnectionError("still down")

    monkeypatch.setattr(worker_daemon, "_connect_for_provider", _always_fails)
    with pytest.raises(ConnectionError):
        worker_daemon._connect_with_recovery("http://x", "local", max_attempts=3, backoff_seconds=0)
    assert len(attempts) == 3  # bounded, not infinite


# 6. A fresh Steel connect always clears stale Singleton locks against the
# SAME configured persistent profile directory -- never a throwaway path.
def test_connect_for_provider_steel_cleans_locks_on_configured_profile_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("DICEPILOT_BROWSER_PROFILE_DIR", str(tmp_path))
    monkeypatch.setattr(worker_daemon, "ensure_steel_session", lambda base_url: {"id": "s", "status": "live"})
    seen_dirs = []
    monkeypatch.setattr(worker_daemon, "clean_stale_singleton_locks", lambda profile_dir: seen_dirs.append(profile_dir) or [])
    monkeypatch.setattr(worker_daemon, "_connect", lambda cdp_url: ("playwright", "page"))

    worker_daemon._connect_for_provider("ws://localhost:3000", "steel")
    assert seen_dirs == [str(tmp_path)]


def test_connect_for_provider_steel_raises_when_steel_api_down(monkeypatch):
    def _boom(base_url):
        raise SteelUnavailableError("down")

    monkeypatch.setattr(worker_daemon, "ensure_steel_session", _boom)
    with pytest.raises(SteelUnavailableError):
        worker_daemon._connect_for_provider("ws://localhost:3000", "steel")


# 17. Local provider never touches Steel at all.
def test_connect_for_provider_local_never_calls_steel(monkeypatch):
    steel_calls = []
    monkeypatch.setattr(worker_daemon, "ensure_steel_session", lambda base_url: steel_calls.append(1))
    monkeypatch.setattr(worker_daemon, "_connect", lambda cdp_url: ("playwright", "page"))

    result = worker_daemon._connect_for_provider("http://127.0.0.1:9333", "local")
    assert result == ("playwright", "page")
    assert steel_calls == []


# 7. Dice auth is rechecked (not assumed) on every fresh connect / idle check.
class _FakePage:
    def goto(self, *a, **kw):
        pass

    def wait_for_load_state(self, *a, **kw):
        pass


class _FakePlaywright:
    def stop(self):
        pass


def test_check_browser_and_auth_reports_online_when_authenticated(monkeypatch):
    monkeypatch.setattr(worker_daemon, "_connect_for_provider", lambda cdp_url, provider: (_FakePlaywright(), _FakePage()))
    import dice_browser.session as session_mod
    from dice_browser.models import BrowserState

    monkeypatch.setattr(session_mod, "detect_challenge", lambda page: None)
    monkeypatch.setattr(session_mod, "classify_authentication", lambda page: BrowserState.ACTIVE)
    assert worker_daemon._check_browser_and_auth("http://x", "local") == "ONLINE"


def test_check_browser_and_auth_reports_auth_required_when_logged_out(monkeypatch):
    monkeypatch.setattr(worker_daemon, "_connect_for_provider", lambda cdp_url, provider: (_FakePlaywright(), _FakePage()))
    import dice_browser.session as session_mod
    from dice_browser.models import BrowserState

    monkeypatch.setattr(session_mod, "detect_challenge", lambda page: None)
    monkeypatch.setattr(session_mod, "classify_authentication", lambda page: BrowserState.AUTH_REQUIRED)
    assert worker_daemon._check_browser_and_auth("http://x", "local") == "AUTH_REQUIRED"


def test_check_browser_and_auth_reports_security_challenge(monkeypatch):
    monkeypatch.setattr(worker_daemon, "_connect_for_provider", lambda cdp_url, provider: (_FakePlaywright(), _FakePage()))
    import dice_browser.session as session_mod
    from dice_browser.models import ChallengeType

    monkeypatch.setattr(session_mod, "detect_challenge", lambda page: ChallengeType.OTP)
    assert worker_daemon._check_browser_and_auth("http://x", "local") == "SECURITY_CHALLENGE"


def test_check_browser_and_auth_reports_disconnected_when_connect_fails(monkeypatch):
    def _boom(cdp_url, provider):
        raise ConnectionError("no session")

    monkeypatch.setattr(worker_daemon, "_connect_for_provider", _boom)
    assert worker_daemon._check_browser_and_auth("http://x", "steel") == "BROWSER_DISCONNECTED"


# 13, 14, 15. reconcile_run_after_disconnect: pre-submit recovers safely,
# post-submit-boundary never blind-retries Submit, and the run itself is
# handed back to PENDING/unclaimed either way -- no duplicate Submit is
# ever possible from this path since it never calls submit_application.
def test_reconcile_run_after_disconnect_requeues_pre_submit_processing():
    job, app_ = _make_job_and_application("TEST Steel PreSubmitDisconnect")
    run = run_registry.create_run([app_["id"]], candidate_id=CANDIDATE)
    try:
        update_application_status(app_["id"], "PROCESSING", worker_id="TEST-worker")
        reconciled = run_registry.reconcile_run_after_disconnect(run["id"])
        assert app_["id"] in reconciled["requeued"]
        refreshed = get_application(app_["id"])
        assert refreshed["status"] == "QUEUED"
        assert refreshed["worker_id"] is None
        assert run_registry.get_run(run["id"])["status"] == "PENDING"
    finally:
        _cleanup(job["id"])


def test_reconcile_run_after_disconnect_never_retries_post_submit_boundary():
    job, app_ = _make_job_and_application("TEST Steel PostSubmitDisconnect")
    run = run_registry.create_run([app_["id"]], candidate_id=CANDIDATE)
    try:
        update_application_status(app_["id"], "PROCESSING", worker_id="TEST-worker")
        update_application_status(app_["id"], "SUBMITTING")
        reconciled = run_registry.reconcile_run_after_disconnect(run["id"])
        assert app_["id"] in reconciled["needs_verification"]
        assert app_["id"] not in reconciled["requeued"]
        refreshed = get_application(app_["id"])
        assert refreshed["status"] == "FAILED_RETRYABLE"
        assert refreshed["error_code"] == "SUBMISSION_UNCERTAIN_AFTER_CRASH"
    finally:
        _cleanup(job["id"])


# Same guarantee, exercised through the daemon's own mid-run exception
# handling: run_worker_for_run raising never crashes the daemon loop, is
# never blind-retried, and writes a RECOVERING heartbeat on the way.
def test_daemon_recovers_from_mid_run_disconnect_without_crashing(monkeypatch):
    worker_id = f"TEST-worker-{uuid.uuid4()}"
    monkeypatch.setenv("DICEPILOT_CANDIDATE_ID", CANDIDATE)
    fake_run = {"id": "TEST-fake-run-steel", "candidate_id": CANDIDATE, "status": "RUNNING", "application_ids": [], "submission_policy": "REQUIRE_CONFIRMATION"}
    reconcile_calls = []

    def _fake_claim(wid, cid):
        return fake_run if not reconcile_calls else None

    def _boom_run(page, run_id, *a, **kw):
        raise ConnectionError("Steel session died mid-run")

    monkeypatch.setattr(run_registry, "claim_next_pending_run", _fake_claim)
    monkeypatch.setattr(run_registry, "reconcile_run_after_disconnect", lambda run_id: reconcile_calls.append(run_id) or {"requeued": [], "needs_verification": []})
    monkeypatch.setattr(worker_daemon, "run_worker_for_run", _boom_run)
    monkeypatch.setattr(worker_daemon, "_connect_with_recovery", lambda *a, **kw: (_FakePlaywright(), _FakePage()))
    try:
        worker_daemon.run_daemon(worker_id, max_iterations=1, poll_interval=0)
        assert reconcile_calls == [fake_run["id"]]
        hb = run_registry.get_latest_heartbeat()
        assert hb["worker_id"] == worker_id
    finally:
        from db.application_repository import get_supabase_client

        get_supabase_client().table("worker_heartbeats").delete().eq("worker_id", worker_id).execute()


# 18. No real Dice mutation anywhere in this file -- every Steel HTTP call
# (requests.get/post) and every Playwright/CDP connection is monkeypatched
# above; nothing here opens a real browser page or calls submit_application.
