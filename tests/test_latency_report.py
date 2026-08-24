"""Phase 7.7: dice_browser.latency_report -- built entirely from the
existing application_events/attention_events tables, against the real,
linked Supabase project (same TEST-prefixed disposable row convention as
the other integration tests).
"""
from __future__ import annotations

import uuid

import pytest

from attention.events import record_inbound, record_outbound
from db.application_repository import add_event, create_job_offer, upsert_dice_job
from db.supabase_client import get_supabase_client
from dice_browser.latency_report import format_latency_report, get_application_timeline

_created_job_ids: list[str] = []


def _make_offer():
    candidate_id = str(uuid.uuid4())
    dice_job_id = f"TEST-{uuid.uuid4()}"
    job = upsert_dice_job({"dice_job_id": dice_job_id, "canonical_url": f"https://dice.com/job/{dice_job_id}", "title": "Latency Test Role"})
    _created_job_ids.append(job["id"])
    return create_job_offer(candidate_id, job["id"])


def _cleanup(job_id: str):
    client = get_supabase_client()
    apps = client.table("applications").select("id").eq("dice_job_id", job_id).execute().data
    for a in apps:
        client.table("application_events").delete().eq("application_id", a["id"]).execute()
        client.table("attention_events").delete().eq("application_id", a["id"]).execute()
        client.table("applications").delete().eq("id", a["id"]).execute()
    client.table("dice_jobs").delete().eq("id", job_id).execute()


@pytest.fixture(autouse=True)
def _cleanup_created_jobs():
    try:
        yield
    finally:
        while _created_job_ids:
            _cleanup(_created_job_ids.pop())


def test_timeline_merges_and_sorts_events_from_both_tables(live_client):
    offer = _make_offer()
    application_id = offer["id"]
    candidate_id = offer["candidate_id"]

    record_inbound(application_id, candidate_id, "TELEGRAM", "JOB_OFFER", "APPLY", str(uuid.uuid4()))
    record_outbound(application_id, candidate_id, "TELEGRAM", "APPLY_ACK", "msg-1")
    add_event(application_id, event_type="worker_claimed", step="CLAIM", message="worker-1")
    add_event(application_id, event_type="job_opened", step="OPEN_JOB", message="https://dice.com/x")

    timeline = get_application_timeline(application_id)

    labels = [e["label"] for e in timeline]
    assert labels == ["APPLY_RECEIVED", "APPLY_ACK_SENT", "WORKER_CLAIMED", "JOB_OPENED"]
    # chronological, not insertion order
    assert timeline == sorted(timeline, key=lambda e: e["at"])


def test_format_latency_report_computes_deltas_and_total(live_client):
    offer = _make_offer()
    application_id = offer["id"]
    candidate_id = offer["candidate_id"]

    record_inbound(application_id, candidate_id, "TELEGRAM", "JOB_OFFER", "APPLY", str(uuid.uuid4()))
    record_outbound(application_id, candidate_id, "TELEGRAM", "APPLY_ACK", "msg-1")

    report = format_latency_report(application_id)

    assert "APPLY_RECEIVED -> APPLY_ACK_SENT" in report
    assert "TOTAL (APPLY_RECEIVED -> APPLY_ACK_SENT)" in report


def test_format_latency_report_handles_no_events(live_client):
    offer = _make_offer()
    report = format_latency_report(offer["id"])
    assert "no events recorded yet" in report
