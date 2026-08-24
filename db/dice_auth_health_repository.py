"""Phase 8A: the durable Dice auth-health signal (dice_auth_health,
one row per candidate) -- what lets the readiness gate answer "do we
currently believe Dice auth is good?" without a live browser check on
every job offer. No browser/Dice.com logic here -- this module only
talks to Postgres, matching db/application_repository.py's own split.

mark_invalid() is the one write path readiness.py must never skip: a
real AUTH_REQUIRED anywhere (pre-offer check or post-Apply execution)
has to immediately flip is_healthy to false, since a stale cached
"healthy" is exactly the failure mode this table exists to prevent.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from db.supabase_client import get_supabase_client


def get_auth_health(candidate_id: str) -> dict[str, Any] | None:
    rows = (
        get_supabase_client()
        .table("dice_auth_health")
        .select("*")
        .eq("candidate_id", candidate_id)
        .execute()
        .data
    )
    return rows[0] if rows else None


def mark_healthy(candidate_id: str) -> dict[str, Any]:
    """Records a POSITIVE, just-happened auth verification. Never call
    this speculatively/optimistically -- only after a real signal
    (classify_authentication returning ACTIVE) was actually observed."""
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "candidate_id": candidate_id,
        "is_healthy": True,
        "last_verified_at": now,
        "last_checked_at": now,
        "invalidated_at": None,
        "invalidated_reason": None,
    }
    result = get_supabase_client().table("dice_auth_health").upsert(payload, on_conflict="candidate_id").execute()
    return result.data[0]


def mark_invalid(candidate_id: str, reason: str) -> dict[str, Any]:
    """Records a known auth failure (e.g. a real AUTH_REQUIRED). This
    is the one write path that must never be skipped or delayed --
    readiness.py's whole cost-control model (cache a recent positive
    result instead of live-checking every job) only stays safe as long
    as a known failure is reflected here immediately."""
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "candidate_id": candidate_id,
        "is_healthy": False,
        "last_checked_at": now,
        "invalidated_at": now,
        "invalidated_reason": reason,
    }
    result = get_supabase_client().table("dice_auth_health").upsert(payload, on_conflict="candidate_id").execute()
    return result.data[0]
