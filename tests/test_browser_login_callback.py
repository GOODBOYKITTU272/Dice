"""Phase F2B (revised): the AUTH_APPROVE:/AUTH_DENY: Telegram callback
handler -- the ownership check (only the exact bound candidate's Telegram
can approve/deny their own challenge) is the load-bearing property here.
"""
from __future__ import annotations

from unittest.mock import patch

from attention.browser_login_callback import handle_login_callback, is_login_callback
from attention.channels import bind_channel
from db.browser_login_challenge import create_challenge, get_challenge


def test_is_login_callback_recognizes_the_namespace():
    assert is_login_callback("AUTH_APPROVE:abc-123") is True
    assert is_login_callback("AUTH_DENY:abc-123") is True
    assert is_login_callback("APPLY:abc-123") is False
    assert is_login_callback("SKIP:abc-123") is False
    assert is_login_callback("ANSWER:some text") is False


def test_approval_sent_only_to_the_exact_bound_candidate(fake_auth_client):
    bind_channel("cand-1", "TELEGRAM", "111", verified=True)
    challenge_id, _secret, _exp = create_challenge("cand-1")

    with patch("attention.browser_login_callback.TelegramProvider") as mock_provider_cls:
        result = handle_login_callback(f"AUTH_APPROVE:{challenge_id}", "111")

    assert result == "auth_approved"
    assert get_challenge(challenge_id)["status"] == "APPROVED"
    mock_provider_cls.assert_called_once_with(chat_id="111")


def test_candidate_a_cannot_approve_candidate_bs_challenge(fake_auth_client):
    bind_channel("cand-a", "TELEGRAM", "111", verified=True)
    bind_channel("cand-b", "TELEGRAM", "222", verified=True)
    challenge_id, _secret, _exp = create_challenge("cand-b")

    with patch("attention.browser_login_callback.TelegramProvider"):
        # cand-a's own Telegram (chat "111") tries to approve cand-b's challenge
        result = handle_login_callback(f"AUTH_APPROVE:{challenge_id}", "111")

    assert result == "auth_rejected_ownership"
    assert get_challenge(challenge_id)["status"] == "PENDING"


def test_unbound_telegram_sender_is_ignored(fake_auth_client):
    challenge_id, _secret, _exp = create_challenge("cand-1")

    result = handle_login_callback(f"AUTH_APPROVE:{challenge_id}", "unbound-chat")

    assert result == "auth_ignored_unknown_sender"
    assert get_challenge(challenge_id)["status"] == "PENDING"


def test_unknown_challenge_id_is_stale(fake_auth_client):
    bind_channel("cand-1", "TELEGRAM", "111", verified=True)

    with patch("attention.browser_login_callback.TelegramProvider"):
        result = handle_login_callback("AUTH_APPROVE:does-not-exist", "111")

    assert result == "auth_stale"


def test_deny_by_owning_candidate_marks_denied(fake_auth_client):
    bind_channel("cand-1", "TELEGRAM", "111", verified=True)
    challenge_id, _secret, _exp = create_challenge("cand-1")

    with patch("attention.browser_login_callback.TelegramProvider"):
        result = handle_login_callback(f"AUTH_DENY:{challenge_id}", "111")

    assert result == "auth_denied"
    assert get_challenge(challenge_id)["status"] == "DENIED"


def test_auth_callback_namespace_never_enters_attentionaction():
    """The structural guarantee, not just a naming convention: nothing in
    this module imports or constructs AttentionAction/NormalizedEvent."""
    import attention.browser_login_callback as mod

    assert "AttentionAction" not in dir(mod)
    assert "handle_event" not in dir(mod)
