"""Phase F2B (revised): the Telegram-approved login challenge -- the
middle link of the trust chain (bootstrap code -> THIS -> customer
session). A challenge is created only for a candidate with an already-
verified Telegram binding, approved only by that exact candidate's
Telegram, and exchanged for a customer session exactly once.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from db.supabase_client import get_supabase_client

DEFAULT_TTL_SECONDS = 300  # 5 minutes, matching the customer access token


class ChallengeNotApprovableError(RuntimeError):
    """Raised when a challenge can't be created, approved, denied, or
    exchanged right now -- one error type covering every reason (doesn't
    exist, wrong candidate, wrong status, expired) so none of these are
    distinguishable from the outside."""


def _hash_secret(raw_secret: str) -> str:
    return hashlib.sha256(raw_secret.encode("utf-8")).hexdigest()


def create_challenge(candidate_id: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> tuple[str, str, str]:
    """Returns (challenge_id, raw_challenge_secret, expires_at_iso). The
    raw secret is returned exactly once and never stored -- only its
    hash is persisted."""
    if not candidate_id:
        raise ValueError("candidate_id is required")

    raw_secret = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)

    client = get_supabase_client()
    result = (
        client.table("browser_login_challenges")
        .insert(
            {
                "candidate_id": candidate_id,
                "challenge_secret_hash": _hash_secret(raw_secret),
                "expires_at": expires_at.isoformat(),
            }
        )
        .execute()
    )
    challenge_id = result.data[0]["id"]
    return challenge_id, raw_secret, expires_at.isoformat()


def get_challenge(challenge_id: str) -> dict | None:
    client = get_supabase_client()
    rows = client.table("browser_login_challenges").select("*").eq("id", challenge_id).execute().data
    return rows[0] if rows else None


def verify_challenge_secret(challenge: dict, raw_secret: str) -> bool:
    if not raw_secret:
        return False
    return hmac.compare_digest(challenge["challenge_secret_hash"], _hash_secret(raw_secret))


def approve_challenge(challenge_id: str) -> dict | None:
    """Atomic PENDING -> APPROVED transition. Returns the updated row, or
    None if the challenge wasn't PENDING/unexpired -- a second approval
    tap is a safe idempotent no-op, never a second state change."""
    client = get_supabase_client()
    result = client.rpc("approve_browser_login_challenge", {"p_challenge_id": challenge_id}).execute()
    rows = result.data or []
    return rows[0] if rows else None


def deny_challenge(challenge_id: str) -> dict | None:
    """Atomic PENDING -> DENIED transition. Denial is final -- once
    DENIED (or already APPROVED/consumed by then), this is a no-op."""
    client = get_supabase_client()
    result = client.rpc("deny_browser_login_challenge", {"p_challenge_id": challenge_id}).execute()
    rows = result.data or []
    return rows[0] if rows else None


def expire_challenge(challenge_id: str) -> dict | None:
    """Phase 13 recovery path: if the Telegram approval message couldn't
    actually be delivered, force the challenge to a terminal state
    immediately rather than leaving it PENDING with no way to ever be
    approved."""
    client = get_supabase_client()
    result = client.rpc("expire_browser_login_challenge", {"p_challenge_id": challenge_id}).execute()
    rows = result.data or []
    return rows[0] if rows else None


def consume_challenge_for_exchange(challenge_id: str) -> dict:
    """Atomic APPROVED -> CONSUMED transition -- the session-exchange
    atomicity guarantee. Raises ChallengeNotApprovableError if the
    challenge isn't APPROVED/unexpired (covers PENDING, DENIED, EXPIRED,
    already-CONSUMED, and unknown ids identically)."""
    client = get_supabase_client()
    result = client.rpc("consume_browser_login_challenge", {"p_challenge_id": challenge_id}).execute()
    rows = result.data or []
    if not rows:
        raise ChallengeNotApprovableError("challenge is not an approved, unexpired, unconsumed challenge")
    return rows[0]
