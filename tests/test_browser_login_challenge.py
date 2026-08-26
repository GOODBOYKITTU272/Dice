"""Phase F2B (revised): the Telegram-approved login challenge -- status
transitions and, above all, the concurrency guarantee on the exchange
step (one APPROVED challenge must never mint two sessions).
"""
from __future__ import annotations

import threading

import pytest

from db.browser_login_challenge import (
    ChallengeNotApprovableError,
    approve_challenge,
    consume_challenge_for_exchange,
    create_challenge,
    deny_challenge,
    expire_challenge,
    get_challenge,
    verify_challenge_secret,
)


def test_challenge_secret_is_random(fake_auth_client):
    _id1, secret1, _exp1 = create_challenge("cand-1")
    _id2, secret2, _exp2 = create_challenge("cand-1")
    assert secret1 != secret2
    assert len(secret1) >= 32


def test_challenge_secret_is_stored_hashed(fake_auth_client):
    challenge_id, secret, _exp = create_challenge("cand-1")
    row = get_challenge(challenge_id)
    assert secret not in row.values()
    assert row["challenge_secret_hash"] != secret


def test_wrong_secret_is_rejected(fake_auth_client):
    challenge_id, _secret, _exp = create_challenge("cand-1")
    row = get_challenge(challenge_id)
    assert verify_challenge_secret(row, "wrong-secret") is False


def test_correct_secret_is_accepted(fake_auth_client):
    challenge_id, secret, _exp = create_challenge("cand-1")
    row = get_challenge(challenge_id)
    assert verify_challenge_secret(row, secret) is True


def test_pending_challenge_cannot_be_exchanged(fake_auth_client):
    challenge_id, _secret, _exp = create_challenge("cand-1")
    with pytest.raises(ChallengeNotApprovableError):
        consume_challenge_for_exchange(challenge_id)


def test_denied_challenge_cannot_be_exchanged(fake_auth_client):
    challenge_id, _secret, _exp = create_challenge("cand-1")
    deny_challenge(challenge_id)
    with pytest.raises(ChallengeNotApprovableError):
        consume_challenge_for_exchange(challenge_id)


def test_deny_is_final_approve_after_deny_is_a_noop(fake_auth_client):
    challenge_id, _secret, _exp = create_challenge("cand-1")
    deny_challenge(challenge_id)
    assert approve_challenge(challenge_id) is None
    assert get_challenge(challenge_id)["status"] == "DENIED"


def test_approval_is_idempotent(fake_auth_client):
    challenge_id, _secret, _exp = create_challenge("cand-1")
    first = approve_challenge(challenge_id)
    second = approve_challenge(challenge_id)
    assert first is not None
    assert second is None  # already APPROVED, not PENDING -- safe no-op
    assert get_challenge(challenge_id)["status"] == "APPROVED"


def test_approved_challenge_exchanges_for_exact_candidate(fake_auth_client):
    challenge_id, _secret, _exp = create_challenge("cand-1")
    approve_challenge(challenge_id)
    consumed = consume_challenge_for_exchange(challenge_id)
    assert consumed["candidate_id"] == "cand-1"
    assert consumed["status"] == "CONSUMED"


def test_exchange_is_one_time_replay_is_rejected(fake_auth_client):
    challenge_id, _secret, _exp = create_challenge("cand-1")
    approve_challenge(challenge_id)
    consume_challenge_for_exchange(challenge_id)
    with pytest.raises(ChallengeNotApprovableError):
        consume_challenge_for_exchange(challenge_id)


def test_expired_challenge_cannot_be_approved(fake_auth_client):
    challenge_id, _secret, _exp = create_challenge("cand-1", ttl_seconds=-1)
    assert approve_challenge(challenge_id) is None


def test_expired_challenge_cannot_be_exchanged(fake_auth_client):
    challenge_id, _secret, _exp = create_challenge("cand-1")
    approve_challenge(challenge_id)
    # Force it into an already-expired state directly, simulating time
    # having passed between approval and exchange -- via the same
    # in-memory fake store the repo functions above already use.
    row = next(r for r in fake_auth_client.tables["browser_login_challenges"] if r["id"] == challenge_id)
    row["expires_at"] = "2000-01-01T00:00:00+00:00"

    with pytest.raises(ChallengeNotApprovableError):
        consume_challenge_for_exchange(challenge_id)


def test_send_failure_recovery_expires_a_pending_challenge(fake_auth_client):
    challenge_id, _secret, _exp = create_challenge("cand-1")
    expire_challenge(challenge_id)
    assert get_challenge(challenge_id)["status"] == "EXPIRED"
    assert approve_challenge(challenge_id) is None


def test_concurrent_exchange_allows_exactly_one_success(fake_auth_client):
    challenge_id, _secret, _exp = create_challenge("cand-1")
    approve_challenge(challenge_id)

    results: list[dict | None] = [None, None]

    def _attempt(i):
        try:
            results[i] = consume_challenge_for_exchange(challenge_id)
        except ChallengeNotApprovableError:
            results[i] = None

    threads = [threading.Thread(target=_attempt, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successes = [r for r in results if r is not None]
    assert len(successes) == 1
