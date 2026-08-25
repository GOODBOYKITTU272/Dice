"""Phase 7.5: primary/secondary channel routing. Telegram is primary,
iMessage secondary, for V1 -- driven entirely by candidate_attention_
channels.is_primary, never a hardcoded channel name here. A provider is
constructed with the candidate's real bound identity (chat_id/contact),
never the env var directly -- that stays only as the last-resort default
inside the provider classes themselves for single-candidate/dev use.
"""
from __future__ import annotations

import os
from typing import Callable

from attention.channels import primary_channel_for_candidate, secondary_channels_for_candidate


def _provider_for_channel_row(row: dict):
    if row["channel"] == "TELEGRAM":
        from attention.providers.telegram import TelegramProvider

        return TelegramProvider(chat_id=row["external_user_id"])
    if row["channel"] == "IMESSAGE":
        from attention.providers.loopmessage import LoopMessageProvider

        return LoopMessageProvider(contact=row["external_user_id"])
    return None


def _channel_configured(channel: str) -> bool:
    # Telegram needs the bot token (env, never the candidate's row) to
    # send anything at all. iMessage (via LoopMessage, Phase 7.11) needs
    # its own API key configured the same way.
    if channel == "TELEGRAM":
        return bool(os.environ.get("TELEGRAM_BOT_TOKEN"))
    if channel == "IMESSAGE":
        return bool(os.environ.get("LOOPMESSAGE_AUTH_KEY"))
    return False


def resolve_primary_provider(candidate_id: str):
    """The candidate's real, bound primary-channel provider, or None if
    none is bound/configured. Public counterpart to send_via_primary_
    with_fallback's own internal lookup -- for callers (Phase M9's
    discovery daemon) that need the provider object itself, e.g. to pass
    into a function whose return value matters and would otherwise be
    discarded by the notify_fn(provider, *args) -> None convention that
    function is built around."""
    primary = primary_channel_for_candidate(candidate_id)
    if primary is not None and _channel_configured(primary["channel"]):
        return _provider_for_channel_row(primary)
    return None


def send_via_primary_with_fallback(candidate_id: str, notify_fn: Callable, *args) -> str:
    """Returns one of: "primary", "fallback", "unknown_retryable",
    "no_channel". Never sends to both for the same event -- a primary
    send that raised is recorded as unknown_retryable (delivery state
    truly isn't known: Telegram may have actually accepted it before the
    exception), never treated as "safe to also try iMessage", which
    would risk spraying duplicate action prompts across channels. Only
    falls back to secondary when the primary channel is structurally
    unavailable (not bound, or its provider isn't configured at all) --
    a known-safe condition, never a reaction to an uncertain send
    failure."""
    primary = primary_channel_for_candidate(candidate_id)
    if primary is not None and _channel_configured(primary["channel"]):
        provider = _provider_for_channel_row(primary)
        try:
            notify_fn(provider, *args)
            return "primary"
        except Exception:
            return "unknown_retryable"

    for row in secondary_channels_for_candidate(candidate_id):
        if not _channel_configured(row["channel"]):
            continue
        provider = _provider_for_channel_row(row)
        try:
            notify_fn(provider, *args)
            return "fallback"
        except Exception:
            return "unknown_retryable"

    return "no_channel"
