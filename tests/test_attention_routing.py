"""Phase 7.5: attention.routing -- primary/secondary channel selection.
Against real Supabase (candidate_attention_channels); provider send
methods are monkeypatched, never real network/OS calls.
"""
from __future__ import annotations

import uuid

import pytest

import attention.routing as routing
from attention.channels import bind_channel
from attention.providers.imessage import IMessageProvider
from attention.providers.telegram import TelegramProvider
from db.supabase_client import get_supabase_client

_created_channel_rows: list[str] = []


@pytest.fixture(autouse=True)
def _cleanup(live_client):
    yield
    client = get_supabase_client()
    while _created_channel_rows:
        client.table("candidate_attention_channels").delete().eq("id", _created_channel_rows.pop()).execute()


def _new_external_id() -> str:
    return f"TEST-{uuid.uuid4()}"


# 23. primary-channel routing chooses Telegram
def test_routes_to_telegram_when_both_bound(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    candidate_id = str(uuid.uuid4())
    tg = bind_channel(candidate_id, "TELEGRAM", _new_external_id())
    im = bind_channel(candidate_id, "IMESSAGE", _new_external_id())
    _created_channel_rows.extend([tg["id"], im["id"]])

    calls = []

    def notify_fn(provider, arg):
        calls.append((provider.channel, arg))

    result = routing.send_via_primary_with_fallback(candidate_id, notify_fn, "app-1")

    assert result == "primary"
    assert calls == [("TELEGRAM", "app-1")]


# 24. secondary fallback can select iMessage
def test_falls_back_to_imessage_when_telegram_unconfigured(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("LOOPMESSAGE_AUTH_KEY", "test-key")
    candidate_id = str(uuid.uuid4())
    tg = bind_channel(candidate_id, "TELEGRAM", _new_external_id())
    im = bind_channel(candidate_id, "IMESSAGE", _new_external_id())
    _created_channel_rows.extend([tg["id"], im["id"]])

    calls = []

    def notify_fn(provider, arg):
        calls.append((provider.channel, arg))

    result = routing.send_via_primary_with_fallback(candidate_id, notify_fn, "app-1")

    assert result == "fallback"
    assert calls == [("IMESSAGE", "app-1")]


# 25. successful primary send does not duplicate to fallback
def test_successful_primary_send_never_also_calls_fallback(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    candidate_id = str(uuid.uuid4())
    tg = bind_channel(candidate_id, "TELEGRAM", _new_external_id())
    im = bind_channel(candidate_id, "IMESSAGE", _new_external_id())
    _created_channel_rows.extend([tg["id"], im["id"]])

    calls = []

    def notify_fn(provider, arg):
        calls.append(provider.channel)

    routing.send_via_primary_with_fallback(candidate_id, notify_fn, "app-1")

    assert calls.count("TELEGRAM") == 1
    assert "IMESSAGE" not in calls


def test_uncertain_primary_failure_never_falls_back(monkeypatch):
    # An exception during the primary send does NOT trigger a fallback
    # attempt -- delivery state is genuinely unknown (Telegram may have
    # already accepted it), so spraying a duplicate to iMessage would
    # risk a real duplicate action prompt.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    candidate_id = str(uuid.uuid4())
    tg = bind_channel(candidate_id, "TELEGRAM", _new_external_id())
    im = bind_channel(candidate_id, "IMESSAGE", _new_external_id())
    _created_channel_rows.extend([tg["id"], im["id"]])

    calls = []

    def flaky_notify_fn(provider, arg):
        calls.append(provider.channel)
        if provider.channel == "TELEGRAM":
            raise RuntimeError("network timeout -- delivery state unknown")

    result = routing.send_via_primary_with_fallback(candidate_id, flaky_notify_fn, "app-1")

    assert result == "unknown_retryable"
    assert calls == ["TELEGRAM"]  # never also tried iMessage


def test_no_channel_bound_returns_no_channel():
    result = routing.send_via_primary_with_fallback(str(uuid.uuid4()), lambda provider, arg: None, "app-1")
    assert result == "no_channel"


def test_primary_provider_uses_the_bound_identity_not_env_var(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    candidate_id = str(uuid.uuid4())
    bound_chat_id = _new_external_id()
    tg = bind_channel(candidate_id, "TELEGRAM", bound_chat_id)
    _created_channel_rows.append(tg["id"])

    captured = {}

    def notify_fn(provider, arg):
        captured["chat_id_override"] = provider._chat_id_override

    routing.send_via_primary_with_fallback(candidate_id, notify_fn, "app-1")

    assert captured["chat_id_override"] == bound_chat_id
