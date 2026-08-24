"""Phase 7.5: attention.consumer -- the actual inbound receiver, tying
channel resolution + link-code onboarding + the existing state machine
together. Against real Supabase; Telegram/iMessage transports themselves
are never touched (fetch_updates/read_new_messages are monkeypatched --
this file proves the DISPATCH logic, not the real network/OS calls,
which test_attention_telegram.py / test_attention_imessage.py already
cover in isolation).
"""
from __future__ import annotations

import subprocess
import uuid

import pytest
import requests

import attention.consumer as consumer
from attention.channels import bind_channel, create_link_code, resolve_candidate_for_identity
from attention.providers.imessage import IMessageProvider
from attention.providers.telegram import TelegramProvider
from db.application_repository import create_job_offer, get_application, upsert_dice_job
from db.supabase_client import get_supabase_client

_created_job_ids: list[str] = []
_created_channel_rows: list[str] = []
_created_codes: list[str] = []


class _FakeTelegramResponse:
    def raise_for_status(self):
        pass

    def json(self):
        return {"ok": True, "result": {"message_id": 1}}


@pytest.fixture(autouse=True)
def _no_real_transports(monkeypatch):
    """Blanket safety net for every test in this file: consumer.py builds
    a REAL, chat_id/contact-bound provider around whatever inbound sender
    it resolves (see process_telegram_update/process_imessage_row) --
    including for the fake identities these tests use -- so any code path
    that ends up SENDING something (Apply/Skip acks, answer confirmations,
    etc.) must never be allowed to reach a real osascript/Messages.app
    call or a real Telegram API call. Real finding: without this, a fake
    test contact like "+15554407359" got a real (failed) iMessage send
    attempt once Apply/Skip started sending acknowledgements."""
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: None)
    monkeypatch.setattr(requests, "post", lambda *a, **kw: _FakeTelegramResponse())


def _make_test_job():
    dice_job_id = f"TEST-{uuid.uuid4()}"
    job = upsert_dice_job({"dice_job_id": dice_job_id, "canonical_url": f"https://dice.com/job/{dice_job_id}", "title": "Consumer Test Role", "company_name": "Test Co"})
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
        client.table("applications").delete().eq("id", aid).execute()
    for run_id in run_ids:
        client.table("application_runs").delete().eq("id", run_id).execute()
    client.table("dice_jobs").delete().eq("id", job_id).execute()


@pytest.fixture(autouse=True)
def _cleanup_all(live_client):
    yield
    client = get_supabase_client()
    while _created_job_ids:
        _cleanup(_created_job_ids.pop())
    while _created_channel_rows:
        client.table("candidate_attention_channels").delete().eq("id", _created_channel_rows.pop()).execute()
    while _created_codes:
        client.table("attention_link_codes").delete().eq("code", _created_codes.pop()).execute()


def _new_chat_id() -> str:
    return str(uuid.uuid4().int % 10_000_000_000)


# ── Telegram: link code onboarding (4) ──────────────────────────────────


def test_telegram_start_command_links_correct_candidate():
    candidate_id = str(uuid.uuid4())
    code = create_link_code(candidate_id, "TELEGRAM")
    _created_codes.append(code)
    chat_id = _new_chat_id()
    provider = TelegramProvider()

    result = consumer.process_telegram_update(provider, {"update_id": 1, "message": {"text": f"/start {code}", "chat": {"id": int(chat_id)}}})

    assert result == "linked"
    assert resolve_candidate_for_identity("TELEGRAM", chat_id) == candidate_id
    row = get_supabase_client().table("candidate_attention_channels").select("id").eq("channel", "TELEGRAM").eq("external_user_id", chat_id).execute().data[0]
    _created_channel_rows.append(row["id"])


def test_telegram_invalid_code_falls_through_without_crashing():
    provider = TelegramProvider()
    result = consumer.process_telegram_update(provider, {"update_id": 2, "message": {"text": "NOTREALLY", "chat": {"id": 555}}})
    # "NOTREALLY" is not 8 hex chars -- not even treated as a code attempt
    assert result == "ignored_unknown_sender"


# 12. unknown Telegram user rejected/ignored safely
def test_telegram_unknown_sender_is_ignored():
    provider = TelegramProvider()
    result = consumer.process_telegram_update(provider, {"update_id": 3, "callback_query": {"data": "APPLY", "message": {"chat": {"id": 999999}}}})
    assert result == "ignored_unknown_sender"


def test_telegram_callback_clears_buttons_on_the_original_message(monkeypatch):
    candidate_id = str(uuid.uuid4())
    chat_id = _new_chat_id()
    row = bind_channel(candidate_id, "TELEGRAM", chat_id, verified=True)
    _created_channel_rows.append(row["id"])
    job = _make_test_job()
    create_job_offer(candidate_id, job["id"])
    provider = TelegramProvider()
    cleared = []
    monkeypatch.setattr(TelegramProvider, "clear_buttons", lambda self, chat_id, message_id: cleared.append((chat_id, message_id)))

    consumer.process_telegram_update(provider, {"update_id": 50, "callback_query": {"id": "cb-1", "data": "APPLY", "message": {"message_id": 777, "chat": {"id": int(chat_id)}}}})

    assert cleared == [(chat_id, "777")]


# 5 & 6. Telegram job offer goes to mapped candidate / Apply maps correctly
def test_telegram_apply_from_bound_sender_is_processed():
    candidate_id = str(uuid.uuid4())
    chat_id = _new_chat_id()
    row = bind_channel(candidate_id, "TELEGRAM", chat_id, verified=True)
    _created_channel_rows.append(row["id"])

    job = _make_test_job()
    offer = create_job_offer(candidate_id, job["id"])
    provider = TelegramProvider()

    result = consumer.process_telegram_update(provider, {"update_id": 10, "callback_query": {"data": "APPLY", "message": {"chat": {"id": int(chat_id)}}}})

    assert result == "processed"
    assert get_application(offer["id"])["status"] == "QUEUED"


# 7. Telegram Skip maps correctly
def test_telegram_skip_from_bound_sender_is_processed():
    candidate_id = str(uuid.uuid4())
    chat_id = _new_chat_id()
    row = bind_channel(candidate_id, "TELEGRAM", chat_id, verified=True)
    _created_channel_rows.append(row["id"])

    job = _make_test_job()
    offer = create_job_offer(candidate_id, job["id"])
    provider = TelegramProvider()

    result = consumer.process_telegram_update(provider, {"update_id": 11, "callback_query": {"data": "SKIP", "message": {"chat": {"id": int(chat_id)}}}})

    assert result == "processed"
    assert get_application(offer["id"])["status"] == "SKIPPED"


# 11. duplicate callback processed once
def test_telegram_duplicate_update_id_processed_once():
    candidate_id = str(uuid.uuid4())
    chat_id = _new_chat_id()
    row = bind_channel(candidate_id, "TELEGRAM", chat_id, verified=True)
    _created_channel_rows.append(row["id"])

    job = _make_test_job()
    offer = create_job_offer(candidate_id, job["id"])
    provider = TelegramProvider()
    raw_update = {"update_id": 20, "callback_query": {"data": "SKIP", "message": {"chat": {"id": int(chat_id)}}}}

    first = consumer.process_telegram_update(provider, raw_update)
    second = consumer.process_telegram_update(provider, raw_update)

    assert first == "processed"
    assert second == "duplicate"
    assert get_application(offer["id"])["status"] == "SKIPPED"


# 13. stale callback rejected -- a Confirm/Edit tap with nothing pending
# for that candidate (e.g. delivered after the question was already
# resolved another way, or just a stray old button) must not crash the
# poll loop -- attention.service.UnresolvableEventError is caught here.
def test_telegram_stale_confirm_callback_is_ignored_not_raised():
    candidate_id = str(uuid.uuid4())
    chat_id = _new_chat_id()
    row = bind_channel(candidate_id, "TELEGRAM", chat_id, verified=True)
    _created_channel_rows.append(row["id"])
    provider = TelegramProvider()

    result = consumer.process_telegram_update(provider, {"update_id": 30, "callback_query": {"data": "CONFIRM", "message": {"chat": {"id": int(chat_id)}}}})

    assert result == "ignored_stale"


# ── polling loop offset derivation ───────────────────────────────────────


def test_poll_telegram_once_uses_offset_derived_from_attention_events(monkeypatch):
    candidate_id = str(uuid.uuid4())
    chat_id = _new_chat_id()
    row = bind_channel(candidate_id, "TELEGRAM", chat_id, verified=True)
    _created_channel_rows.append(row["id"])
    job = _make_test_job()
    offer = create_job_offer(candidate_id, job["id"])

    # The offset is derived from the real MAX(external_message_id) already
    # recorded for TELEGRAM in this (shared) Supabase project -- a fixed
    # low fake update_id would break the moment any other TELEGRAM
    # activity (including real live bot use) has ever been recorded.
    # Picking one safely above whatever's already there, and having the
    # fake mimic real getUpdates semantics (only return ids >= offset),
    # keeps this test correct regardless of that real, ever-growing state.
    fake_update_id = (consumer._last_seen_external_id("TELEGRAM") or 0) + 1000

    provider = TelegramProvider()
    captured_offsets = []

    def fake_fetch_updates(offset=None, timeout=0):
        captured_offsets.append(offset)
        if offset is None or offset <= fake_update_id:
            return [{"update_id": fake_update_id, "callback_query": {"data": "SKIP", "message": {"chat": {"id": int(chat_id)}}}}]
        return []

    monkeypatch.setattr(provider, "fetch_updates", fake_fetch_updates)

    first_results = consumer.poll_telegram_once(provider)
    second_results = consumer.poll_telegram_once(provider)

    assert first_results == ["processed"]
    # What's actually under test: the second poll's offset picks up right
    # after the update_id just processed -- not any particular value for
    # the first poll's offset, which depends on however much real
    # TELEGRAM history already exists in this shared project.
    assert captured_offsets[1] == fake_update_id + 1
    assert second_results == []


# ── iMessage (14-21) ──────────────────────────────────────────────────────


def test_imessage_link_code_links_correct_candidate():
    candidate_id = str(uuid.uuid4())
    code = create_link_code(candidate_id, "IMESSAGE")
    _created_codes.append(code)
    contact = f"+1555{uuid.uuid4().int % 10_000_000:07d}"
    provider = IMessageProvider()

    result = consumer.process_imessage_row(provider, contact, {"rowid": 1, "text": code})

    assert result == "linked"
    row = get_supabase_client().table("candidate_attention_channels").select("id").eq("channel", "IMESSAGE").eq("external_user_id", contact).execute().data[0]
    _created_channel_rows.append(row["id"])


# 16. unknown iMessage sender ignored
def test_imessage_unknown_sender_ignored():
    provider = IMessageProvider()
    result = consumer.process_imessage_row(provider, "+15559999999", {"rowid": 2, "text": "APPLY"})
    assert result == "ignored_unknown_sender"


# 17 & 18. iMessage APPLY / SKIP maps correctly
def test_imessage_apply_from_bound_sender_is_processed():
    candidate_id = str(uuid.uuid4())
    contact = f"+1555{uuid.uuid4().int % 10_000_000:07d}"
    row = bind_channel(candidate_id, "IMESSAGE", contact, verified=True)
    _created_channel_rows.append(row["id"])
    job = _make_test_job()
    offer = create_job_offer(candidate_id, job["id"])
    provider = IMessageProvider()

    result = consumer.process_imessage_row(provider, contact, {"rowid": 3, "text": "APPLY"})

    assert result == "processed"
    assert get_application(offer["id"])["status"] == "QUEUED"


def test_imessage_skip_from_bound_sender_is_processed():
    candidate_id = str(uuid.uuid4())
    contact = f"+1555{uuid.uuid4().int % 10_000_000:07d}"
    row = bind_channel(candidate_id, "IMESSAGE", contact, verified=True)
    _created_channel_rows.append(row["id"])
    job = _make_test_job()
    offer = create_job_offer(candidate_id, job["id"])
    provider = IMessageProvider()

    result = consumer.process_imessage_row(provider, contact, {"rowid": 4, "text": "SKIP"})

    assert result == "processed"
    assert get_application(offer["id"])["status"] == "SKIPPED"


def test_imessage_duplicate_rowid_processed_once():
    candidate_id = str(uuid.uuid4())
    contact = f"+1555{uuid.uuid4().int % 10_000_000:07d}"
    row = bind_channel(candidate_id, "IMESSAGE", contact, verified=True)
    _created_channel_rows.append(row["id"])
    job = _make_test_job()
    offer = create_job_offer(candidate_id, job["id"])
    provider = IMessageProvider()
    raw_row = {"rowid": 5, "text": "SKIP"}

    first = consumer.process_imessage_row(provider, contact, raw_row)
    second = consumer.process_imessage_row(provider, contact, raw_row)

    assert first == "processed"
    assert second == "duplicate"
