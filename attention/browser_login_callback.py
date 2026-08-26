"""Phase F2B (revised): handles Telegram's AUTH_APPROVE:/AUTH_DENY:
callback taps -- deliberately never touching AttentionAction,
attention/service.py, or any Apply/Skip/Confirm/Edit/NEEDS_INPUT
handling. attention/consumer.py checks for this namespace and returns
early before any of that job-offer machinery ever runs.
"""
from __future__ import annotations

from attention.channels import resolve_candidate_for_identity
from attention.providers.telegram import TelegramProvider
from db.browser_login_challenge import approve_challenge, deny_challenge, get_challenge

_APPROVE_PREFIX = "AUTH_APPROVE:"
_DENY_PREFIX = "AUTH_DENY:"


def is_login_callback(data: str) -> bool:
    return data.startswith(_APPROVE_PREFIX) or data.startswith(_DENY_PREFIX)


def handle_login_callback(data: str, chat_id: str) -> str:
    """Returns one of: "auth_approved", "auth_denied",
    "auth_ignored_unknown_sender", "auth_rejected_ownership",
    "auth_stale". Never raises -- a stale/foreign/expired tap is always a
    safe no-op here, same convention as the job-offer callback handling
    in attention/consumer.py."""
    is_approve = data.startswith(_APPROVE_PREFIX)
    challenge_id = data[len(_APPROVE_PREFIX):] if is_approve else data[len(_DENY_PREFIX):]

    # The ownership check: this Telegram chat must already be the
    # verified binding for the exact candidate the challenge was created
    # for. candidate_id on a challenge is immutable once created, so this
    # read has no race to protect against -- only the status transition
    # below needs (and gets, via the atomic RPC-backed approve_challenge/
    # deny_challenge) real DB-level atomicity.
    tapping_candidate_id = resolve_candidate_for_identity("TELEGRAM", chat_id)
    if tapping_candidate_id is None:
        return "auth_ignored_unknown_sender"

    challenge = get_challenge(challenge_id)
    if challenge is None:
        return "auth_stale"
    if challenge["candidate_id"] != tapping_candidate_id:
        return "auth_rejected_ownership"

    provider = TelegramProvider(chat_id=chat_id)

    if is_approve:
        approved = approve_challenge(challenge_id)
        if approved is None:
            return "auth_stale"
        provider.send_login_approved_confirmation()
        return "auth_approved"

    denied = deny_challenge(challenge_id)
    if denied is None:
        return "auth_stale"
    provider.send_login_denied_confirmation()
    return "auth_denied"
