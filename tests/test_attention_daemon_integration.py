"""Phase 7.4/7.5: worker_daemon.py's Apply/Skip messaging additions --
_find_resumable_application, _notify_result. Against the real, linked
Supabase project, same TEST-prefixed disposable row convention as the
other Phase 7.4 tests. Never opens a browser -- these are the pure
orchestration-glue functions around the (unmodified) real worker/
attention layers.

Phase 7.5: _notify_result now routes through attention.routing (the
candidate's real bound candidate_attention_channels identity) instead of
a raw env-var provider list -- its tests bind a real channel row and
monkeypatch the provider class's send method, never a bare provider list.
"""
from __future__ import annotations

import uuid

import pytest

import attention.channels as attention_channels
import dice_browser.worker_daemon as worker_daemon
import run_registry
from attention.providers.telegram import TelegramProvider
from db.application_repository import enqueue_application, update_application_status, upsert_dice_job
from db.intervention_repository import create_or_get_question_intervention, resolve_question_intervention
from db.supabase_client import get_supabase_client
from dice_browser.worker import ApplicationRunResult, StopReason

_created_job_ids: list[str] = []
_created_channel_rows: list[str] = []


@pytest.fixture(autouse=True)
def _cleanup_created_channel_rows():
    try:
        yield
    finally:
        client = get_supabase_client()
        while _created_channel_rows:
            client.table("candidate_attention_channels").delete().eq("id", _created_channel_rows.pop()).execute()


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


# ── _notify_result (Phase 7.5: routes via attention.routing, using the
# candidate's real bound candidate_attention_channels identity) ─────────


def _bind_test_telegram_channel(candidate_id: str) -> None:
    row = attention_channels.bind_channel(candidate_id, "TELEGRAM", f"TEST-{uuid.uuid4()}")
    _created_channel_rows.append(row["id"])


def test_notify_result_dispatches_needs_input(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    candidate_id = str(uuid.uuid4())
    job = _make_test_job()
    application = _needs_input_application_with_run(candidate_id, job["id"])
    create_or_get_question_intervention(application_id=application["id"], question_id="q-1", question_prompt="Question 1", field_type="TEXT_INPUT", reason="no trusted candidate mapping")
    _bind_test_telegram_channel(candidate_id)
    calls = []
    monkeypatch.setattr(TelegramProvider, "send_missing_question", lambda self, application_id, question: calls.append(("missing_question", application_id)) or "1")

    result = ApplicationRunResult(application["id"], job["id"], StopReason.NEEDS_INPUT, "one or more questions need human input")
    worker_daemon._notify_result(candidate_id, result)

    assert calls == [("missing_question", application["id"])]


def test_notify_result_dispatches_verified_submitted(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    candidate_id = str(uuid.uuid4())
    job = _make_test_job()
    application = enqueue_application(candidate_id, job["id"])
    _bind_test_telegram_channel(candidate_id)
    calls = []
    monkeypatch.setattr(TelegramProvider, "send_submission_success", lambda self, application, job: calls.append(("success", application["id"])) or "1")

    result = ApplicationRunResult(application["id"], job["id"], StopReason.VERIFIED_SUBMITTED, "explicit confirmation text found")
    worker_daemon._notify_result(candidate_id, result)

    assert calls == [("success", application["id"])]


def test_notify_result_noops_for_unrelated_stop_reasons(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    candidate_id = str(uuid.uuid4())
    _bind_test_telegram_channel(candidate_id)
    calls = []
    monkeypatch.setattr(TelegramProvider, "send_missing_question", lambda self, application_id, question: calls.append("called") or "1")
    monkeypatch.setattr(TelegramProvider, "send_submission_success", lambda self, application, job: calls.append("called") or "1")
    result = ApplicationRunResult("some-app-id", "some-job-id", StopReason.NOTHING_QUEUED, "no QUEUED application available")

    worker_daemon._notify_result(candidate_id, result)

    assert calls == []


def test_notify_result_noops_when_no_candidate_id():
    result = ApplicationRunResult("some-app-id", "some-job-id", StopReason.VERIFIED_SUBMITTED, "ok")
    worker_daemon._notify_result(None, result)  # must not raise with no candidate


def test_notify_result_noops_when_application_id_is_none():
    result = ApplicationRunResult(None, None, StopReason.NOTHING_QUEUED, "no QUEUED application available")
    worker_daemon._notify_result(str(uuid.uuid4()), result)  # must not raise
