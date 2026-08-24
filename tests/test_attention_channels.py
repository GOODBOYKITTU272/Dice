"""Phase 7.5: candidate <-> messaging-channel identity. Against the
real, linked Supabase project, same TEST-prefixed disposable convention
as the rest of the Phase 7.4/7.5 suite.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from attention.channels import (
    ChannelIdentityConflictError,
    bind_channel,
    consume_link_code,
    create_link_code,
    primary_channel_for_candidate,
    resolve_candidate_for_identity,
    secondary_channels_for_candidate,
)
from db.supabase_client import get_supabase_client

_created_codes: list[str] = []
_created_channel_rows: list[str] = []


def _cleanup_row(candidate_id: str, channel: str, external_user_id: str):
    client = get_supabase_client()
    rows = client.table("candidate_attention_channels").select("id").eq("channel", channel).eq("external_user_id", external_user_id).execute().data
    for r in rows:
        client.table("candidate_attention_channels").delete().eq("id", r["id"]).execute()


@pytest.fixture(autouse=True)
def _cleanup(live_client):
    yield
    client = get_supabase_client()
    while _created_codes:
        code = _created_codes.pop()
        client.table("attention_link_codes").delete().eq("code", code).execute()
    while _created_channel_rows:
        row_id = _created_channel_rows.pop()
        client.table("candidate_attention_channels").delete().eq("id", row_id).execute()


def _new_external_id() -> str:
    return f"TEST-{uuid.uuid4()}"


# 1. candidate can bind Telegram identity
def test_bind_channel_creates_mapping():
    candidate_id = str(uuid.uuid4())
    external_id = _new_external_id()
    row = bind_channel(candidate_id, "TELEGRAM", external_id, destination="@testuser")
    _created_channel_rows.append(row["id"])

    assert row["candidate_id"] == candidate_id
    assert row["channel"] == "TELEGRAM"
    assert row["is_primary"] is True  # TELEGRAM defaults to primary


# 2. duplicate Telegram identity cannot bind incorrectly
def test_bind_channel_refuses_to_rebind_to_a_different_candidate():
    candidate_a = str(uuid.uuid4())
    candidate_b = str(uuid.uuid4())
    external_id = _new_external_id()
    row = bind_channel(candidate_a, "TELEGRAM", external_id)
    _created_channel_rows.append(row["id"])

    with pytest.raises(ChannelIdentityConflictError):
        bind_channel(candidate_b, "TELEGRAM", external_id)

    # original binding must be unchanged
    assert resolve_candidate_for_identity("TELEGRAM", external_id) == candidate_a


def test_bind_channel_updates_same_candidate_without_conflict():
    candidate_id = str(uuid.uuid4())
    external_id = _new_external_id()
    row = bind_channel(candidate_id, "TELEGRAM", external_id, destination="@old")
    _created_channel_rows.append(row["id"])

    updated = bind_channel(candidate_id, "TELEGRAM", external_id, destination="@new")
    assert updated["destination"] == "@new"
    assert updated["id"] == row["id"]


# 3. Telegram linking token expires/consumes safely
def test_link_code_expires():
    candidate_id = str(uuid.uuid4())
    code = create_link_code(candidate_id, "TELEGRAM", ttl_minutes=-1)  # already expired
    _created_codes.append(code)

    result = consume_link_code(code, "TELEGRAM", _new_external_id())
    assert result is None


def test_link_code_cannot_be_consumed_twice():
    candidate_id = str(uuid.uuid4())
    code = create_link_code(candidate_id, "TELEGRAM")
    _created_codes.append(code)
    external_id = _new_external_id()

    first = consume_link_code(code, "TELEGRAM", external_id)
    bound_row = get_supabase_client().table("candidate_attention_channels").select("id").eq("channel", "TELEGRAM").eq("external_user_id", external_id).execute().data
    if bound_row:
        _created_channel_rows.append(bound_row[0]["id"])
    second = consume_link_code(code, "TELEGRAM", _new_external_id())

    assert first == candidate_id
    assert second is None


def test_link_code_wrong_channel_rejected():
    candidate_id = str(uuid.uuid4())
    code = create_link_code(candidate_id, "TELEGRAM")
    _created_codes.append(code)

    result = consume_link_code(code, "IMESSAGE", _new_external_id())
    assert result is None


# 4. Telegram /start maps correct candidate
def test_consume_link_code_binds_correct_candidate_and_marks_verified():
    candidate_id = str(uuid.uuid4())
    code = create_link_code(candidate_id, "TELEGRAM")
    _created_codes.append(code)
    external_id = _new_external_id()

    result = consume_link_code(code, "TELEGRAM", external_id)

    assert result == candidate_id
    client = get_supabase_client()
    row = client.table("candidate_attention_channels").select("*").eq("channel", "TELEGRAM").eq("external_user_id", external_id).execute().data[0]
    _created_channel_rows.append(row["id"])
    assert row["verified_at"] is not None


def test_unknown_code_returns_none():
    assert consume_link_code("NOTAREALCODE", "TELEGRAM", _new_external_id()) is None


# 14. candidate can bind iMessage identity
def test_bind_imessage_identity_defaults_to_secondary():
    candidate_id = str(uuid.uuid4())
    external_id = _new_external_id()
    row = bind_channel(candidate_id, "IMESSAGE", external_id, destination="+15551234567")
    _created_channel_rows.append(row["id"])
    assert row["is_primary"] is False


# unknown sender protection (15 / 16)
def test_resolve_candidate_for_identity_returns_none_for_unknown_sender():
    assert resolve_candidate_for_identity("TELEGRAM", _new_external_id()) is None


# ── primary/secondary routing (23) ──────────────────────────────────────


def test_primary_channel_prefers_telegram_over_imessage():
    candidate_id = str(uuid.uuid4())
    tg = bind_channel(candidate_id, "TELEGRAM", _new_external_id())
    im = bind_channel(candidate_id, "IMESSAGE", _new_external_id())
    _created_channel_rows.extend([tg["id"], im["id"]])

    primary = primary_channel_for_candidate(candidate_id)
    assert primary["channel"] == "TELEGRAM"

    secondary = secondary_channels_for_candidate(candidate_id)
    assert [s["channel"] for s in secondary] == ["IMESSAGE"]


def test_primary_channel_is_the_only_bound_channel_when_just_one_exists():
    candidate_id = str(uuid.uuid4())
    im = bind_channel(candidate_id, "IMESSAGE", _new_external_id())
    _created_channel_rows.append(im["id"])

    primary = primary_channel_for_candidate(candidate_id)
    assert primary["channel"] == "IMESSAGE"


def test_primary_channel_none_when_nothing_bound():
    assert primary_channel_for_candidate(str(uuid.uuid4())) is None


def test_disabled_channel_excluded_from_primary_selection():
    candidate_id = str(uuid.uuid4())
    tg = bind_channel(candidate_id, "TELEGRAM", _new_external_id())
    _created_channel_rows.append(tg["id"])
    client = get_supabase_client()
    client.table("candidate_attention_channels").update({"is_enabled": False}).eq("id", tg["id"]).execute()

    assert primary_channel_for_candidate(candidate_id) is None
