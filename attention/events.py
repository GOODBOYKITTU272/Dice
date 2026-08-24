"""Phase 7.4: durable idempotency + audit log for attention_events
(supabase/migrations/20260824010000_attention_service.sql). No business
logic here -- attention.service is the only caller.
"""
from __future__ import annotations

from typing import Any

from db.supabase_client import get_supabase_client

# message_types where the same OUTBOUND message must never be sent twice
# for the same application+channel -- matches the DB's own partial
# unique index (attention_events_outbound_once_idx). MISSING_QUESTION and
# ANSWER_CONFIRMATION are deliberately excluded: they legitimately repeat
# across different questions on the same application.
_OUTBOUND_ONCE_TYPES = {"JOB_OFFER", "SUBMISSION_SUCCESS", "SUBMISSION_FAILURE"}


def already_sent_outbound(application_id: str, channel: str, message_type: str) -> bool:
    if message_type not in _OUTBOUND_ONCE_TYPES:
        return False
    client = get_supabase_client()
    rows = (
        client.table("attention_events")
        .select("id")
        .eq("application_id", application_id)
        .eq("channel", channel)
        .eq("message_type", message_type)
        .eq("direction", "OUTBOUND")
        .execute()
        .data
    )
    return bool(rows)


def record_outbound(
    application_id: str,
    candidate_id: str,
    channel: str,
    message_type: str,
    external_message_id: str | None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    client = get_supabase_client()
    result = (
        client.table("attention_events")
        .insert(
            {
                "application_id": application_id,
                "candidate_id": candidate_id,
                "channel": channel,
                "direction": "OUTBOUND",
                "message_type": message_type,
                "external_message_id": external_message_id,
                "payload": payload,
            }
        )
        .execute()
    )
    return result.data[0]


def already_processed_inbound(channel: str, external_message_id: str) -> bool:
    """The one hard idempotency guarantee: the same provider-native event
    id is never acted on twice, regardless of application/action."""
    client = get_supabase_client()
    rows = (
        client.table("attention_events")
        .select("id")
        .eq("channel", channel)
        .eq("external_message_id", external_message_id)
        .eq("direction", "INBOUND")
        .execute()
        .data
    )
    return bool(rows)


def record_inbound(
    application_id: str,
    candidate_id: str,
    channel: str,
    message_type: str,
    action: str,
    external_message_id: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    client = get_supabase_client()
    result = (
        client.table("attention_events")
        .insert(
            {
                "application_id": application_id,
                "candidate_id": candidate_id,
                "channel": channel,
                "direction": "INBOUND",
                "message_type": message_type,
                "action": action,
                "external_message_id": external_message_id,
                "payload": payload,
            }
        )
        .execute()
    )
    return result.data[0]


def latest_inbound_answer(application_id: str, question_id: str) -> Any | None:
    """The most recent raw ANSWER text this candidate gave for
    question_id on this application -- what a CONFIRM resolves against.
    Never itself written to interventions.answer; that only happens once
    CONFIRM arrives (attention.service.handle_confirm)."""
    client = get_supabase_client()
    rows = (
        client.table("attention_events")
        .select("payload, created_at")
        .eq("application_id", application_id)
        .eq("direction", "INBOUND")
        .eq("action", "ANSWER")
        .execute()
        .data
        or []
    )
    matching = [r for r in rows if (r.get("payload") or {}).get("question_id") == question_id]
    if not matching:
        return None
    matching.sort(key=lambda r: r.get("created_at") or "")
    return matching[-1]["payload"].get("raw_answer")


def latest_active_question_id(application_id: str) -> str | None:
    """The question_id of the most recent outbound MISSING_QUESTION sent
    for this application -- what an inbound ANSWER/CONFIRM/EDIT with no
    explicit question_id (e.g. a plain-text iMessage reply) is attributed
    to. Only one question is ever active at a time by construction (the
    sequential missing-question design), so "most recent" is unambiguous."""
    client = get_supabase_client()
    rows = (
        client.table("attention_events")
        .select("payload, created_at")
        .eq("application_id", application_id)
        .eq("direction", "OUTBOUND")
        .eq("message_type", "MISSING_QUESTION")
        .execute()
        .data
        or []
    )
    if not rows:
        return None
    rows.sort(key=lambda r: r.get("created_at") or "")
    return (rows[-1].get("payload") or {}).get("question_id")
