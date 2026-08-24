"""Phase 7.5: durable candidate <-> messaging-channel identity
(supabase/migrations/20260825010000_candidate_attention_channels.sql).
Never stores bot/API secrets -- those stay environment-only
(TELEGRAM_BOT_TOKEN, etc.); this table only ever holds the mapping
itself (candidate_id, channel, provider-native external_user_id).
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from db.supabase_client import get_supabase_client

_LINK_CODE_TTL_MINUTES = 10
_PRIMARY_BY_DEFAULT = {"TELEGRAM": True, "IMESSAGE": False}


class ChannelIdentityConflictError(RuntimeError):
    """Raised when an external_user_id already belongs to a DIFFERENT
    candidate -- a channel identity must never silently be re-bound to a
    new candidate; that would let one Telegram/iMessage account start
    controlling a different candidate's applications."""


def create_link_code(candidate_id: str, channel: str, ttl_minutes: int = _LINK_CODE_TTL_MINUTES) -> str:
    """Short-lived, single-use code for the "/start CODE" (Telegram) /
    plain-text "CODE" (iMessage) onboarding flow. Scoped to exactly one
    candidate+channel at creation time -- consuming it is the only thing
    that actually creates the durable mapping."""
    code = secrets.token_hex(4).upper()
    client = get_supabase_client()
    client.table("attention_link_codes").insert(
        {
            "code": code,
            "candidate_id": candidate_id,
            "channel": channel,
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)).isoformat(),
        }
    ).execute()
    return code


def consume_link_code(code: str, channel: str, external_user_id: str, destination: str | None = None) -> str | None:
    """Validates the code (exists, right channel, not expired, not
    already consumed), marks it consumed, and binds external_user_id to
    its candidate_id -- verified, since completing a fresh link code is
    itself the verification. Returns the candidate_id, or None if the
    code is invalid/expired/already used (never raises for a bad code --
    that's an expected, safe outcome, not a bug)."""
    client = get_supabase_client()
    rows = client.table("attention_link_codes").select("*").eq("code", code).eq("channel", channel).execute().data
    if not rows:
        return None
    row = rows[0]
    if row["consumed_at"] is not None:
        return None
    if row["expires_at"] < datetime.now(timezone.utc).isoformat():
        return None

    client.table("attention_link_codes").update({"consumed_at": datetime.now(timezone.utc).isoformat()}).eq("code", code).execute()
    bind_channel(row["candidate_id"], channel, external_user_id, destination=destination, verified=True)
    return row["candidate_id"]


def bind_channel(
    candidate_id: str,
    channel: str,
    external_user_id: str,
    destination: str | None = None,
    verified: bool = False,
    is_primary: bool | None = None,
) -> dict[str, Any]:
    """Creates or updates the (channel, external_user_id) mapping.
    Refuses (ChannelIdentityConflictError) if that exact identity is
    already bound to a DIFFERENT candidate -- never silently re-binds."""
    client = get_supabase_client()
    existing = (
        client.table("candidate_attention_channels")
        .select("*")
        .eq("channel", channel)
        .eq("external_user_id", external_user_id)
        .execute()
        .data
    )
    if existing and existing[0]["candidate_id"] != candidate_id:
        raise ChannelIdentityConflictError(
            f"{channel} identity is already bound to a different candidate"
        )

    payload: dict[str, Any] = {
        "candidate_id": candidate_id,
        "channel": channel,
        "external_user_id": external_user_id,
        "destination": destination,
        "is_primary": is_primary if is_primary is not None else _PRIMARY_BY_DEFAULT.get(channel, False),
    }
    if verified:
        payload["verified_at"] = datetime.now(timezone.utc).isoformat()

    if existing:
        result = (
            client.table("candidate_attention_channels")
            .update(payload)
            .eq("id", existing[0]["id"])
            .execute()
        )
    else:
        result = client.table("candidate_attention_channels").insert(payload).execute()
    return result.data[0]


def resolve_candidate_for_identity(channel: str, external_user_id: str) -> str | None:
    """The unknown-sender guard: None means "no candidate is bound to
    this identity" -- callers must treat that as "ignore this message",
    never as "fall back to the one configured candidate"."""
    client = get_supabase_client()
    rows = (
        client.table("candidate_attention_channels")
        .select("candidate_id")
        .eq("channel", channel)
        .eq("external_user_id", external_user_id)
        .eq("is_enabled", True)
        .execute()
        .data
    )
    return rows[0]["candidate_id"] if rows else None


def primary_channel_for_candidate(candidate_id: str) -> dict[str, Any] | None:
    """The channel to send a notification through first -- Telegram
    ahead of iMessage when both are bound and enabled (V1's stated
    policy), otherwise whichever single channel is bound."""
    client = get_supabase_client()
    rows = (
        client.table("candidate_attention_channels")
        .select("*")
        .eq("candidate_id", candidate_id)
        .eq("is_enabled", True)
        .execute()
        .data
        or []
    )
    if not rows:
        return None
    rows.sort(key=lambda r: (not r["is_primary"], r["channel"]))
    return rows[0]


def secondary_channels_for_candidate(candidate_id: str) -> list[dict[str, Any]]:
    primary = primary_channel_for_candidate(candidate_id)
    client = get_supabase_client()
    rows = (
        client.table("candidate_attention_channels")
        .select("*")
        .eq("candidate_id", candidate_id)
        .eq("is_enabled", True)
        .execute()
        .data
        or []
    )
    return [r for r in rows if primary is None or r["id"] != primary["id"]]
