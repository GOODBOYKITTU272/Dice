"""Phase F2B (revised): the Dice-owned signed session token. The load-
bearing property is that verify_session_token rejects every way a token
can be wrong -- bad signature, expired, wrong issuer/audience, malformed
-- with the same InvalidTokenError, never partial trust.
"""
from __future__ import annotations

import time

import pytest

import db.customer_identity as customer_identity
from db.customer_identity import (
    InvalidTokenError,
    MissingSigningSecretError,
    issue_session_token,
    verify_session_token,
)


@pytest.fixture(autouse=True)
def _configured_secret(monkeypatch):
    monkeypatch.setenv("DICE_CUSTOMER_SESSION_SECRET", "test-secret-do-not-use-in-prod")


def test_valid_token_resolves_the_candidate_id_it_was_issued_for():
    token = issue_session_token("candidate-abc")
    assert verify_session_token(token) == "candidate-abc"


def test_missing_token_is_rejected():
    with pytest.raises(InvalidTokenError):
        verify_session_token("")


def test_malformed_token_is_rejected():
    with pytest.raises(InvalidTokenError):
        verify_session_token("not-a-real-token")


def test_tampered_signature_is_rejected():
    token = issue_session_token("candidate-abc")
    payload_b64, _signature_b64 = token.split(".")
    tampered = f"{payload_b64}.wrong-signature"
    with pytest.raises(InvalidTokenError):
        verify_session_token(tampered)


def test_tampered_payload_is_rejected_even_with_original_signature():
    """The actual attack this exists to stop: swap candidate_id in the
    payload but keep the original signature -- must fail, not silently
    resolve to the tampered candidate_id."""
    token = issue_session_token("candidate-abc")
    _payload_b64, signature_b64 = token.split(".")

    forged_payload_b64 = issue_session_token("candidate-victim").split(".")[0]
    forged = f"{forged_payload_b64}.{signature_b64}"

    with pytest.raises(InvalidTokenError):
        verify_session_token(forged)


def test_expired_token_is_rejected(monkeypatch):
    token = issue_session_token("candidate-abc", ttl_seconds=1)
    original_time = time.time
    monkeypatch.setattr(time, "time", lambda: original_time() + 10)
    with pytest.raises(InvalidTokenError):
        verify_session_token(token)


def test_wrong_issuer_is_rejected(monkeypatch):
    monkeypatch.setattr(customer_identity, "ISSUER", "someone-else")
    token = issue_session_token("candidate-abc")
    monkeypatch.setattr(customer_identity, "ISSUER", "applywizz-dice")
    with pytest.raises(InvalidTokenError):
        verify_session_token(token)


def test_wrong_audience_is_rejected(monkeypatch):
    monkeypatch.setattr(customer_identity, "AUDIENCE", "someone-elses-service")
    token = issue_session_token("candidate-abc")
    monkeypatch.setattr(customer_identity, "AUDIENCE", "dice-me-routes")
    with pytest.raises(InvalidTokenError):
        verify_session_token(token)


def test_token_signed_with_a_different_secret_is_rejected(monkeypatch):
    token = issue_session_token("candidate-abc")
    monkeypatch.setenv("DICE_CUSTOMER_SESSION_SECRET", "a-completely-different-secret")
    with pytest.raises(InvalidTokenError):
        verify_session_token(token)


def test_missing_signing_secret_fails_closed(monkeypatch):
    monkeypatch.delenv("DICE_CUSTOMER_SESSION_SECRET", raising=False)
    with pytest.raises(MissingSigningSecretError):
        verify_session_token("anything.at-all")


def test_default_ttl_is_five_minutes_or_less():
    assert customer_identity.DEFAULT_TTL_SECONDS <= 300


def test_signing_secret_never_appears_in_the_issued_token():
    token = issue_session_token("candidate-abc")
    assert "test-secret-do-not-use-in-prod" not in token
