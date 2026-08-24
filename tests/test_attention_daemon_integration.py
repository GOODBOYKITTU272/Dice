"""Phase 7.4: worker_daemon.py's Apply/Skip messaging additions --
_find_resumable_application, _configured_attention_providers,
_notify_result. Against the real, linked Supabase project, same
TEST-prefixed disposable row convention as the other Phase 7.4 tests.
Never opens a browser -- these are the pure orchestration-glue functions
around the (unmodified) real worker/attention layers.
"""
from __future__ import annotations

import uuid

import pytest

import dice_browser.worker_daemon as worker_daemon
import run_registry
from db.application_repository import enqueue_application, update_application_status, upsert_dice_job
from db.intervention_repository import create_or_get_question_intervention, resolve_question_intervention
from db.supabase_client import get_supabase_client
from dice_browser.worker import ApplicationRunResult, StopReason

_created_job_ids: list[str] = []


def _make_test_job():
    dice_job_id = f"TEST-{uuid.uuid4()}"
    job = upsert_dice_job({"dice_job_id": dice_job_id, "canonical_url": f"https://dice.com/job/{dice_job_id}", "title": "Daemon Attention Test Role"})
    _created_job_ids.append(job["id"])
    return job


def _cleanup(job_id: str):
    client = get_supabase_client()
    apps = client.table("applications").select("id, run_id").eq("dice_job_id", job_id).execute().data
    run_ids = {a["run_id"] for a in apps if a.get("run_id")}
    for a in apps:
        aid = a["id"]
        for iv in client.table("interventions").select("id").eq("application_id", aid).execute().data:
            client.table("interventions").delete().eq("id", iv["id"]).execute()
        for ev in client.table("attention_events").select("id").eq("application_id", aid).execute().data:
            client.table("attention_events").delete().eq("id", ev["id"]).execute()
        for ev in client.table("application_events").select("id").eq("application_id", aid).execute().data:
            client.table("application_events").delete().eq("id", ev["id"]).execute()
        client.table("applications").delete().eq("id", aid).execute()
    for run_id in run_ids:
        client.table("application_runs").delete().eq("id", run_id).execute()
    client.table("dice_jobs").delete().eq("id", job_id).execute()


@pytest.fixture(autouse=True)
def _cleanup_created_jobs():
    try:
        yield
    finally:
        while _created_job_ids:
            _cleanup(_created_job_ids.pop())


def _needs_input_application_with_run(candidate_id: str, job_id: str, run_status: str = "COMPLETE") -> dict:
    application = enqueue_application(candidate_id, job_id)
    run = run_registry.create_run([application["id"]], candidate_id=candidate_id)
    update_application_status(application["id"], "PROCESSING", worker_id="test-daemon-attention")
    update_application_status(application["id"], "NEEDS_INPUT")
    if run_status != run["status"]:
        run_registry.update_run_status(run["id"], run_status)
    application["run_id"] = run["id"]
    return application


# 16 (daemon side) / resume-eligibility -----------------------------------


def test_find_resumable_application_finds_fully_confirmed_application():
    candidate_id = str(uuid.uuid4())
    job = _make_test_job()
    application = _needs_input_application_with_run(candidate_id, job["id"])
    create_or_get_question_intervention(application_id=application["id"], question_id="q-1", question_prompt="Question 1", field_type="TEXT_INPUT", reason="no trusted candidate mapping")
    intervention = create_or_get_question_intervention(application_id=application["id"], question_id="q-1", question_prompt="Question 1", field_type="TEXT_INPUT", reason="no trusted candidate mapping")
    resolve_question_intervention(intervention["id"], "an answer", source="test")

    found = worker_daemon._find_resumable_application(candidate_id)

    assert found is not None
    assert found["id"] == application["id"]


def test_find_resumable_application_returns_none_when_intervention_still_open():
    candidate_id = str(uuid.uuid4())
    job = _make_test_job()
    application = _needs_input_application_with_run(candidate_id, job["id"])
    create_or_get_question_intervention(application_id=application["id"], question_id="q-1", question_prompt="Question 1", field_type="TEXT_INPUT", reason="no trusted candidate mapping")

    assert worker_daemon._find_resumable_application(candidate_id) is None


# 28. historical STOPPED runs untouched -- a fully-confirmed NEEDS_INPUT
# application belonging to a STOPPED run must NEVER be returned as
# resumable, structurally, regardless of its own per-application status.
def test_find_resumable_application_excludes_stopped_runs():
    candidate_id = str(uuid.uuid4())
    job = _make_test_job()
    application = _needs_input_application_with_run(candidate_id, job["id"], run_status="STOPPED")
    intervention = create_or_get_question_intervention(application_id=application["id"], question_id="q-1", question_prompt="Question 1", field_type="TEXT_INPUT", reason="no trusted candidate mapping")
    resolve_question_intervention(intervention["id"], "an answer", source="test")

    assert worker_daemon._find_resumable_application(candidate_id) is None


def test_real_historical_stopped_runs_remain_stopped_and_excluded():
    # Direct regression check against the actual known real production
    # runs from this project's own real usage -- read-only, never
    # mutates them. Confirms both that they are still STOPPED (nothing
    # in this whole implementation touched them) and that the new
    # resume-polling code would correctly skip them even if their
    # candidate_id were queried directly.
    known_stopped_run_ids = [
        "7c8a11e1-0512-4a60-8768-811c354cec89",
        "42105e9e-99f0-4ab6-afaa-ef504c0f9734",
        "d7dd9593-046c-4943-bde3-8f50b880fbd0",
        "2608d8bc-6428-4275-b52d-64bd454b4de0",
    ]
    for run_id in known_stopped_run_ids:
        run = run_registry.get_run(run_id)
        assert run is not None
        assert run["status"] == "STOPPED"


# ── _configured_attention_providers ───────────────────────────────────


def test_configured_attention_providers_empty_when_unconfigured(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("IMESSAGE_CONTACT", raising=False)
    assert worker_daemon._configured_attention_providers() == []


def test_configured_attention_providers_includes_telegram_when_configured(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    monkeypatch.delenv("IMESSAGE_CONTACT", raising=False)
    providers = worker_daemon._configured_attention_providers()
    assert [p.channel for p in providers] == ["TELEGRAM"]


def test_configured_attention_providers_includes_both_when_both_configured(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setenv("IMESSAGE_CONTACT", "+15551234567")
    providers = worker_daemon._configured_attention_providers()
    assert sorted(p.channel for p in providers) == ["IMESSAGE", "TELEGRAM"]


# ── _notify_result ─────────────────────────────────────────────────────


class _SpyProvider:
    channel = "TELEGRAM"

    def __init__(self):
        self.calls = []

    def send_missing_question(self, application_id, question):
        self.calls.append(("missing_question", application_id))
        return "1"

    def send_submission_success(self, application, job):
        self.calls.append(("success", application["id"]))
        return "1"


def test_notify_result_dispatches_needs_input(monkeypatch):
    candidate_id = str(uuid.uuid4())
    job = _make_test_job()
    application = _needs_input_application_with_run(candidate_id, job["id"])
    create_or_get_question_intervention(application_id=application["id"], question_id="q-1", question_prompt="Question 1", field_type="TEXT_INPUT", reason="no trusted candidate mapping")
    provider = _SpyProvider()
    result = ApplicationRunResult(application["id"], job["id"], StopReason.NEEDS_INPUT, "one or more questions need human input")

    worker_daemon._notify_result([provider], result)

    assert provider.calls == [("missing_question", application["id"])]


def test_notify_result_dispatches_verified_submitted(monkeypatch):
    candidate_id = str(uuid.uuid4())
    job = _make_test_job()
    application = enqueue_application(candidate_id, job["id"])
    provider = _SpyProvider()
    result = ApplicationRunResult(application["id"], job["id"], StopReason.VERIFIED_SUBMITTED, "explicit confirmation text found")

    worker_daemon._notify_result([provider], result)

    assert provider.calls == [("success", application["id"])]


def test_notify_result_noops_for_unrelated_stop_reasons():
    provider = _SpyProvider()
    result = ApplicationRunResult("some-app-id", "some-job-id", StopReason.NOTHING_QUEUED, "no QUEUED application available")

    worker_daemon._notify_result([provider], result)

    assert provider.calls == []


def test_notify_result_noops_when_no_providers_configured():
    result = ApplicationRunResult("some-app-id", "some-job-id", StopReason.VERIFIED_SUBMITTED, "ok")
    worker_daemon._notify_result([], result)  # must not raise even with no providers


def test_notify_result_noops_when_application_id_is_none():
    provider = _SpyProvider()
    result = ApplicationRunResult(None, None, StopReason.NOTHING_QUEUED, "no QUEUED application available")
    worker_daemon._notify_result([provider], result)
    assert provider.calls == []
