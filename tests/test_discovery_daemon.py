"""Phase M9: dice_browser.discovery_daemon -- always-on discovery, the
last manual step removed after Phase M8's live single-user hardening.
dice.discovery.run_discovery and readiness.offer_job_if_ready are always
mocked here (no real Dice.com scraping, no real Browserless session) --
their own correctness is already covered by test_discovery.py and
test_readiness.py respectively. Real Supabase is used only where pacing/
dedup correctness genuinely depends on real row state (_unresolved_offer_
count, _internal_job_id).
"""
from __future__ import annotations

import threading
import time
import uuid

import pytest

import dice_browser.discovery_daemon as discovery_daemon
from db.application_repository import create_job_offer, update_application_status, upsert_dice_job
from db.supabase_client import get_supabase_client

_created_job_ids: list[str] = []


@pytest.fixture(autouse=True)
def _cleanup(live_client):
    yield
    client = get_supabase_client()
    while _created_job_ids:
        job_id = _created_job_ids.pop()
        for a in client.table("applications").select("id").eq("dice_job_id", job_id).execute().data:
            client.table("applications").delete().eq("id", a["id"]).execute()
        client.table("dice_jobs").delete().eq("id", job_id).execute()


def _make_job(external_id: str | None = None, is_easy_apply=True, c2c_status="LIKELY") -> dict:
    external_id = external_id or f"DISCOVERY-DAEMON-TEST-{uuid.uuid4()}"
    job = upsert_dice_job(
        {
            "dice_job_id": external_id,
            "canonical_url": f"https://dice.com/job-detail/{external_id}",
            "title": "Discovery Daemon Test Role",
            "company_name": "Test Co",
            "c2c_status": c2c_status,
            "is_easy_apply": is_easy_apply,
        }
    )
    _created_job_ids.append(job["id"])
    return job


def _discovery_row(job: dict, qualified: bool = True) -> dict:
    """Matches the shape dice.discovery.run_discovery() actually returns
    -- dice_job_id here is the EXTERNAL id (job['dice_job_id']), never
    dice_jobs.id, mirroring the real function exactly."""
    return {"dice_job_id": job["dice_job_id"], "title": job["title"], "company_name": job["company_name"], "is_qualified": qualified}


class _FakeProvider:
    channel = "TELEGRAM"


# ── 1. daemon starts / 3. interval configurable ──────────────────────────


def test_daemon_starts_and_runs_bounded_iterations(monkeypatch):
    calls = []
    monkeypatch.setattr(discovery_daemon, "run_one_discovery_cycle", lambda *a, **kw: calls.append(1) or {"inspected": 0, "qualified": 0, "offers_produced": 0, "held": 0, "skipped_capacity": 0, "no_channel": 0, "error": None, "duration_seconds": 0})

    discovery_daemon.run_daemon("candidate-x", interval_seconds=0, max_iterations=3)

    assert len(calls) == 3


def test_interval_seconds_reads_env_var(monkeypatch):
    monkeypatch.setenv(discovery_daemon._INTERVAL_ENV_VAR, "300")
    assert discovery_daemon.resolve_interval_seconds() == 300


def test_interval_seconds_falls_back_to_default_when_unset(monkeypatch):
    monkeypatch.delenv(discovery_daemon._INTERVAL_ENV_VAR, raising=False)
    assert discovery_daemon.resolve_interval_seconds() == discovery_daemon.DEFAULT_INTERVAL_SECONDS


def test_interval_seconds_enforces_a_sane_minimum_floor(monkeypatch):
    monkeypatch.setenv(discovery_daemon._INTERVAL_ENV_VAR, "5")
    assert discovery_daemon.resolve_interval_seconds() == discovery_daemon._MIN_INTERVAL_SECONDS


# ── 2. canonical discovery function is invoked ───────────────────────────


def test_cycle_invokes_the_canonical_run_discovery_function(monkeypatch):
    calls = []

    def _fake_run_discovery(role, max_results, location, printer=None):
        calls.append((role, max_results, location))
        return []

    monkeypatch.setattr("dice.discovery.run_discovery", _fake_run_discovery)

    discovery_daemon.run_one_discovery_cycle("candidate-x", role="Java Developer", max_results=7, location="United States")

    assert calls == [("Java Developer", 7, "United States")]


# ── 4. overlapping cycles are prevented ──────────────────────────────────


def test_overlapping_cycles_never_run_concurrently(monkeypatch):
    concurrent = {"count": 0, "max": 0}
    lock = threading.Lock()

    def _slow_cycle(*a, **kw):
        with lock:
            concurrent["count"] += 1
            concurrent["max"] = max(concurrent["max"], concurrent["count"])
        time.sleep(0.15)
        with lock:
            concurrent["count"] -= 1
        return {"inspected": 0, "qualified": 0, "offers_produced": 0, "held": 0, "skipped_capacity": 0, "no_channel": 0, "error": None, "duration_seconds": 0}

    monkeypatch.setattr(discovery_daemon, "run_one_discovery_cycle", _slow_cycle)

    t1 = threading.Thread(target=discovery_daemon.run_daemon, args=("candidate-x",), kwargs={"interval_seconds": 0, "max_iterations": 1})
    t2 = threading.Thread(target=discovery_daemon.run_daemon, args=("candidate-x",), kwargs={"interval_seconds": 0, "max_iterations": 1})
    t1.start()
    time.sleep(0.02)
    t2.start()
    t1.join()
    t2.join()

    assert concurrent["max"] == 1  # never two cycles in flight at once


def test_a_locked_cycle_is_skipped_not_blocked_forever(monkeypatch):
    calls = []
    monkeypatch.setattr(discovery_daemon, "run_one_discovery_cycle", lambda *a, **kw: calls.append(1) or {})
    discovery_daemon._cycle_lock.acquire()
    try:
        discovery_daemon.run_daemon("candidate-x", interval_seconds=0, max_iterations=1)
    finally:
        discovery_daemon._cycle_lock.release()

    assert calls == []  # the cycle never ran -- the lock was held


# ── 5. transient failure does not kill the daemon ────────────────────────


def test_cycle_catches_a_discovery_failure_and_reports_it():
    def _boom(role, max_results, location, printer=None):
        raise ConnectionError("Dice search unreachable")

    import dice.discovery

    original = dice.discovery.run_discovery
    dice.discovery.run_discovery = _boom
    try:
        summary = discovery_daemon.run_one_discovery_cycle("candidate-x")
    finally:
        dice.discovery.run_discovery = original

    assert summary["error"] is not None
    assert "ConnectionError" in summary["error"]


def test_run_daemon_does_not_double_catch_beyond_the_cycle_boundary(monkeypatch):
    """The real resilience guarantee lives inside run_one_discovery_cycle
    itself (proven above) -- run_daemon deliberately does NOT add a
    second, broader try/except around it, which would silently mask a
    real programming bug instead of surfacing it."""
    monkeypatch.setattr(discovery_daemon, "run_one_discovery_cycle", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("a bug that bypassed the cycle's own error handling")))

    with pytest.raises(RuntimeError):
        discovery_daemon.run_daemon("candidate-x", interval_seconds=0, max_iterations=1)


# ── 6 & 7. duplicate protection / max-unresolved pacing ──────────────────


def test_a_blocked_offer_is_never_retried_within_the_same_cycle_as_a_new_row(monkeypatch):
    job = _make_job()
    monkeypatch.setattr("dice.discovery.run_discovery", lambda role, max_results, location, printer=None: [_discovery_row(job)])
    calls = []
    monkeypatch.setattr("readiness.offer_job_if_ready", lambda provider, cid, jid: calls.append(jid) or {"offered": False, "blocker": "DUPLICATE_APPLICATION"})
    monkeypatch.setattr("attention.routing.resolve_primary_provider", lambda cid: _FakeProvider())

    summary = discovery_daemon.run_one_discovery_cycle(str(uuid.uuid4()))

    assert calls == [job["id"]]  # went through the real gate exactly once -- never bypassed or retried
    assert summary["held"] == 1
    assert summary["offers_produced"] == 0


def _advance_to(application_id: str, target_status: str) -> None:
    """Walks the real, existing status machine (db.application_repository.
    STATUS_TRANSITIONS) from AWAITING_USER_DECISION to any terminal/non-
    capacity status a real application could genuinely reach -- never
    writes a status directly, so this is exactly as strict as production
    (an invalid path here would raise InvalidStatusTransitionError, same
    as in production)."""
    if target_status == "SKIPPED":
        update_application_status(application_id, "SKIPPED")
        return
    update_application_status(application_id, "QUEUED")
    if target_status == "QUEUED":
        return
    update_application_status(application_id, "PROCESSING", worker_id="test-discovery-daemon-worker")
    if target_status == "PROCESSING":
        return
    if target_status == "NEEDS_INPUT":
        update_application_status(application_id, "NEEDS_INPUT")
        return
    if target_status == "FAILED_RETRYABLE":
        update_application_status(application_id, "FAILED_RETRYABLE")
        return
    if target_status == "FAILED":
        update_application_status(application_id, "FAILED")
        return
    if target_status == "SUBMITTED":
        update_application_status(application_id, "SUBMITTING")
        update_application_status(application_id, "SUBMITTED")
        return
    raise ValueError(f"no known path to {target_status!r} for this test helper")


# 3. capacity count must use canonical actionable state only -- every
# status a real application can be in AFTER the original Apply/Skip
# decision, or after it was never actioned but has since moved on, must
# NOT count toward the max-2 unresolved-card cap. Only a genuine, still-
# pending AWAITING_USER_DECISION counts.
@pytest.mark.parametrize("status", ["SKIPPED", "QUEUED", "PROCESSING", "NEEDS_INPUT", "FAILED_RETRYABLE", "FAILED", "SUBMITTED"])
def test_unresolved_capacity_count_excludes_every_non_awaiting_status(status):
    candidate_id = str(uuid.uuid4())
    app = create_job_offer(candidate_id, _make_job()["id"])
    _advance_to(app["id"], status)

    assert discovery_daemon._unresolved_offer_count(candidate_id) == 0


def test_unresolved_capacity_count_includes_only_genuine_awaiting_decision():
    candidate_id = str(uuid.uuid4())
    create_job_offer(candidate_id, _make_job()["id"])  # left untouched -- genuinely still pending

    assert discovery_daemon._unresolved_offer_count(candidate_id) == 1


def test_max_unresolved_offers_holds_extra_eligible_jobs(monkeypatch):
    candidate_id = str(uuid.uuid4())
    job_a = _make_job()
    job_b = _make_job()
    create_job_offer(candidate_id, job_a["id"])  # 1 already outstanding
    create_job_offer(candidate_id, job_b["id"])  # 2 already outstanding -- at capacity

    job_c = _make_job()
    monkeypatch.setattr("dice.discovery.run_discovery", lambda role, max_results, location, printer=None: [_discovery_row(job_c)])
    calls = []
    monkeypatch.setattr("readiness.offer_job_if_ready", lambda provider, cid, jid: calls.append(jid) or {"offered": True})

    summary = discovery_daemon.run_one_discovery_cycle(candidate_id)

    assert calls == []  # never even attempted -- capacity was already full
    assert summary["skipped_capacity"] == 1


def test_capacity_skipped_job_is_not_lost_and_offers_once_capacity_frees_and_it_recurs(monkeypatch):
    """A job held only for capacity is never given an application row --
    the SAME job re-discovered on a later cycle, once capacity has freed
    up, is evaluated fresh through the real gate and offered exactly
    once. Never a second/duplicate offer for it later still."""
    candidate_id = str(uuid.uuid4())
    blocker_app = create_job_offer(candidate_id, _make_job()["id"])  # the one occupying capacity
    job = _make_job()
    monkeypatch.setattr("dice.discovery.run_discovery", lambda role, max_results, location, printer=None: [_discovery_row(job)])
    monkeypatch.setattr("attention.routing.resolve_primary_provider", lambda cid: _FakeProvider())
    calls = []
    monkeypatch.setattr("readiness.offer_job_if_ready", lambda provider, cid, jid: calls.append(jid) or {"offered": True})

    # Cycle 1: capacity full (max_unresolved_offers=1 for this test) -- the
    # job is discovered and persisted (already true via _make_job/upsert
    # inside run_discovery, mocked here) but never offered.
    first = discovery_daemon.run_one_discovery_cycle(candidate_id, max_unresolved_offers=1)
    assert first["skipped_capacity"] == 1
    assert calls == []

    # The blocking card gets resolved (Skip/Apply -- whatever the real
    # user decision is), freeing capacity.
    from db.application_repository import update_application_status

    update_application_status(blocker_app["id"], "SKIPPED")

    # Cycle 2: the SAME job is re-discovered (still live on Dice) --
    # capacity is now free, so it's evaluated fresh and offered.
    second = discovery_daemon.run_one_discovery_cycle(candidate_id, max_unresolved_offers=1)
    assert second["skipped_capacity"] == 0
    assert second["offers_produced"] == 1
    assert calls == [job["id"]]  # offered exactly once, not twice, across both cycles


def test_capacity_check_is_reevaluated_between_jobs_in_the_same_cycle(monkeypatch):
    """Proves pacing is live, not a snapshot taken once at cycle start --
    an offer produced by job A must count toward capacity before job B is
    considered, in the same cycle."""
    candidate_id = str(uuid.uuid4())
    create_job_offer(candidate_id, _make_job()["id"])  # 1 already outstanding -- one slot left
    job_a = _make_job()
    job_b = _make_job()
    monkeypatch.setattr(
        "dice.discovery.run_discovery",
        lambda role, max_results, location, printer=None: [_discovery_row(job_a), _discovery_row(job_b)],
    )
    monkeypatch.setattr("attention.routing.resolve_primary_provider", lambda cid: _FakeProvider())

    def _fake_offer(provider, cid, jid):
        if jid == job_a["id"]:
            create_job_offer(cid, jid)  # simulates the real side effect -- fills the last slot
            return {"offered": True}
        return {"offered": True}  # would only be reached if capacity was (wrongly) not rechecked

    monkeypatch.setattr("readiness.offer_job_if_ready", _fake_offer)

    summary = discovery_daemon.run_one_discovery_cycle(candidate_id)

    assert summary["offers_produced"] == 1
    assert summary["skipped_capacity"] == 1  # job_b correctly held -- capacity was full by then


# ── 8. stale-auth eligible job self-recovers (thin daemon-level proof --
# the recovery mechanism itself is exhaustively covered in
# test_readiness.py's Phase M8C suite) ────────────────────────────────────


def test_cycle_counts_a_self_recovered_offer_as_produced(monkeypatch):
    job = _make_job()
    monkeypatch.setattr("dice.discovery.run_discovery", lambda role, max_results, location, printer=None: [_discovery_row(job)])
    monkeypatch.setattr("attention.routing.resolve_primary_provider", lambda cid: _FakeProvider())
    # offer_job_if_ready itself is what runs auth self-recovery internally
    # (Phase M8C) -- from the daemon's perspective it's opaque, it just
    # sees the eventual result.
    monkeypatch.setattr("readiness.offer_job_if_ready", lambda provider, cid, jid: {"offered": True, "application_id": "app-1"})

    summary = discovery_daemon.run_one_discovery_cycle(str(uuid.uuid4()))

    assert summary["offers_produced"] == 1


# ── 9. blocked job remains reconsiderable ────────────────────────────────


def test_blocked_job_creates_no_application_row(monkeypatch):
    job = _make_job()
    monkeypatch.setattr("dice.discovery.run_discovery", lambda role, max_results, location, printer=None: [_discovery_row(job)])
    monkeypatch.setattr("attention.routing.resolve_primary_provider", lambda cid: _FakeProvider())
    monkeypatch.setattr("readiness.offer_job_if_ready", lambda provider, cid, jid: {"offered": False, "blocker": "AUTH_REQUIRED"})

    discovery_daemon.run_one_discovery_cycle(str(uuid.uuid4()))

    existing = get_supabase_client().table("applications").select("id").eq("dice_job_id", job["id"]).execute().data
    assert existing == []  # the daemon itself never creates a row -- only a successful offer_job_if_ready does


# ── 10. historical/stale backlog is never dumped at startup ─────────────


def test_cycle_never_evaluates_jobs_outside_this_runs_discovery_result(monkeypatch):
    _make_job()  # a real, pre-existing dice_jobs row -- NOT returned by run_discovery below
    monkeypatch.setattr("dice.discovery.run_discovery", lambda role, max_results, location, printer=None: [])
    calls = []
    monkeypatch.setattr("readiness.offer_job_if_ready", lambda provider, cid, jid: calls.append(jid) or {"offered": True})

    summary = discovery_daemon.run_one_discovery_cycle("candidate-x")

    assert calls == []  # zero jobs touched -- the pre-existing row was never swept
    assert summary["inspected"] == 0


# ── 11 & 13. structural boundary: no submission code, no local filesystem
# dependency anywhere in this module ─────────────────────────────────────


def test_discovery_daemon_never_touches_submission_or_the_local_filesystem():
    import inspect

    source = inspect.getsource(discovery_daemon)
    lowered = source.lower()
    assert "submit" not in lowered
    assert "playwright" not in lowered
    assert "open(" not in source  # no local file reads/writes anywhere in this module


# ── 12. discovery only ever produces an offer/decision state, never a
# submission -- proven end to end with the real offer path (readiness.
# offer_job_if_ready itself, unmocked) against a real fixture job/
# candidate, mirroring test_readiness.py's own convention.
def test_a_produced_offer_is_awaiting_user_decision_never_submitted(monkeypatch):
    import run_registry
    from db import dice_auth_health_repository

    candidate_id = str(uuid.uuid4())
    job = _make_job()

    monkeypatch.setattr(run_registry, "worker_status", lambda: {"online": True, "status": "ONLINE"})
    monkeypatch.setenv("DICEPILOT_BROWSER_PROVIDER", "local")
    import readiness

    monkeypatch.setattr(readiness, "resume_exists_in_storage", lambda cid: True)
    dice_auth_health_repository.mark_healthy(candidate_id)

    monkeypatch.setattr("dice.discovery.run_discovery", lambda role, max_results, location, printer=None: [_discovery_row(job)])

    class _NoopProvider(_FakeProvider):
        def send_job_offer(self, application, job):
            return "msg-1"

    monkeypatch.setattr("attention.routing.resolve_primary_provider", lambda cid: _NoopProvider())

    summary = discovery_daemon.run_one_discovery_cycle(candidate_id)

    assert summary["offers_produced"] == 1
    app = get_supabase_client().table("applications").select("status").eq("candidate_id", candidate_id).eq("dice_job_id", job["id"]).execute().data[0]
    assert app["status"] == "AWAITING_USER_DECISION"
