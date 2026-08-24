"""Phase 7.5: the actual inbound receiver -- Telegram long-polling
(getUpdates; no publicly reachable backend exists yet for a webhook) and
local iMessage polling (chat.db). Ties together link-code onboarding,
unknown-sender rejection, and the existing, unmodified attention.service
state machine. No cursor table -- the "last seen" position for each
channel is derived from attention_events itself (already the durable
record of every inbound event ever processed), so a restart never
re-processes or silently skips updates.
"""
from __future__ import annotations

from attention.channels import consume_link_code, resolve_candidate_for_identity
from attention.events import already_processed_inbound
from attention.providers.imessage import IMessageProvider, read_new_messages
from attention.providers.telegram import TelegramProvider
from attention.service import UnresolvableEventError, handle_event
from db.supabase_client import get_supabase_client


def _last_seen_external_id(channel: str) -> int | None:
    client = get_supabase_client()
    rows = (
        client.table("attention_events")
        .select("external_message_id")
        .eq("channel", channel)
        .eq("direction", "INBOUND")
        .execute()
        .data
        or []
    )
    ids = [int(r["external_message_id"]) for r in rows if r.get("external_message_id") and r["external_message_id"].lstrip("-").isdigit()]
    return max(ids) if ids else None


def _extract_link_code_attempt(text: str | None) -> str | None:
    text = (text or "").strip()
    if text.upper().startswith("/START"):
        text = text[len("/start"):].strip()
    if len(text) == 8 and all(c in "0123456789ABCDEF" for c in text.upper()):
        return text.upper()
    return None


def _try_consume_as_link_code(channel: str, text: str | None, external_user_id: str) -> str | None:
    """Returns the candidate_id if `text` was a valid, unconsumed link
    code for this channel -- None either if it doesn't even look like a
    code, or if it does but isn't a real/valid one (in which case the
    caller must fall through to normal processing, never silently drop
    the message just because it happened to look code-shaped)."""
    code = _extract_link_code_attempt(text)
    if code is None:
        return None
    return consume_link_code(code, channel, external_user_id)


def process_telegram_update(provider: TelegramProvider, raw_update: dict) -> str:
    """Returns one of: "linked", "ignored_unknown_sender", "processed",
    "duplicate", "ignored_no_op", "ignored_stale". Never raises for a
    bad/unattributable/stale inbound message -- that's always a safe
    no-op, never a crash (a stale callback, e.g. tapping Confirm on a
    question already resolved another way, surfaces as attention.
    service.UnresolvableEventError -- caught here rather than left to
    kill the whole poll loop)."""
    external_message_id = str(raw_update["update_id"])
    chat_id = provider.extract_chat_id(raw_update)
    if chat_id is None:
        return "ignored_no_op"

    text = (raw_update.get("message") or {}).get("text")
    linked_candidate_id = _try_consume_as_link_code("TELEGRAM", text, chat_id) if text else None
    if linked_candidate_id is not None:
        return "linked"

    candidate_id = resolve_candidate_for_identity("TELEGRAM", chat_id)
    if candidate_id is None:
        return "ignored_unknown_sender"

    if already_processed_inbound("TELEGRAM", external_message_id):
        return "duplicate"

    event = provider.parse_inbound(raw_update)
    try:
        handle_event(provider, event, candidate_id)
    except UnresolvableEventError:
        return "ignored_stale"
    return "processed"


def poll_telegram_once(provider: TelegramProvider) -> list[str]:
    offset = _last_seen_external_id("TELEGRAM")
    updates = provider.fetch_updates(offset=(offset + 1) if offset is not None else None)
    return [process_telegram_update(provider, u) for u in updates]


def process_imessage_row(provider: IMessageProvider, contact: str, row: dict) -> str:
    external_message_id = str(row["rowid"])
    text = row.get("text")

    linked_candidate_id = _try_consume_as_link_code("IMESSAGE", text, contact)
    if linked_candidate_id is not None:
        return "linked"

    candidate_id = resolve_candidate_for_identity("IMESSAGE", contact)
    if candidate_id is None:
        return "ignored_unknown_sender"

    if already_processed_inbound("IMESSAGE", external_message_id):
        return "duplicate"

    event = provider.parse_inbound(row)
    try:
        handle_event(provider, event, candidate_id)
    except UnresolvableEventError:
        return "ignored_stale"
    return "processed"


def poll_imessage_once(provider: IMessageProvider, contact: str) -> list[str]:
    offset = _last_seen_external_id("IMESSAGE")
    rows = read_new_messages(contact, since_rowid=offset or 0)
    return [process_imessage_row(provider, contact, row) for row in rows]
