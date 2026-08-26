"""Phase F2B (revised): operator-issued browser bootstrap codes -- the
first link of the trust chain. The load-bearing properties are that the
raw code is never persisted (only its hash) and that consumption is
exactly-once even under concurrent attempts.
"""
from __future__ import annotations

import threading

import pytest

from db.browser_bootstrap import BootstrapCodeInvalidError, consume_bootstrap_code, issue_bootstrap_code


def test_issued_code_has_sufficient_entropy(fake_auth_client):
    raw_code, _expires_at = issue_bootstrap_code("cand-1")
    # secrets.token_urlsafe(24) -> 32 base64url chars for 24 random bytes
    # (192 bits) -- comfortably unguessable, not sequential/short.
    assert len(raw_code) >= 32


def test_raw_code_is_not_stored(fake_auth_client):
    raw_code, _expires_at = issue_bootstrap_code("cand-1")
    stored = fake_auth_client.tables["browser_bootstrap_codes"][0]
    assert raw_code not in stored.values()
    assert stored["code_hash"] != raw_code


def test_valid_code_resolves_exact_candidate(fake_auth_client):
    raw_code, _expires_at = issue_bootstrap_code("cand-1")
    assert consume_bootstrap_code(raw_code) == "cand-1"


def test_invalid_code_is_rejected(fake_auth_client):
    with pytest.raises(BootstrapCodeInvalidError):
        consume_bootstrap_code("never-issued")


def test_expired_code_is_rejected(fake_auth_client):
    raw_code, _expires_at = issue_bootstrap_code("cand-1", ttl_minutes=-1)
    with pytest.raises(BootstrapCodeInvalidError):
        consume_bootstrap_code(raw_code)


def test_consumed_code_cannot_be_reused(fake_auth_client):
    raw_code, _expires_at = issue_bootstrap_code("cand-1")
    consume_bootstrap_code(raw_code)
    with pytest.raises(BootstrapCodeInvalidError):
        consume_bootstrap_code(raw_code)


def test_concurrent_consume_allows_exactly_one_success(fake_auth_client):
    raw_code, _expires_at = issue_bootstrap_code("cand-1")
    results: list[str | None] = [None, None]

    def _attempt(i):
        try:
            results[i] = consume_bootstrap_code(raw_code)
        except BootstrapCodeInvalidError:
            results[i] = None

    threads = [threading.Thread(target=_attempt, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successes = [r for r in results if r is not None]
    assert len(successes) == 1
