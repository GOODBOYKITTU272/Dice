"""Phase F2B (revised): operator-issued, one-time browser bootstrap codes
-- the very first step of the trust chain (bootstrap code -> Telegram
approval -> customer session). Not a notification channel and
deliberately not built on attention_link_codes -- see the migration's
own header comment for why.

issue_bootstrap_code is never called from an HTTP route. It exists for
the same trust model already used by db.dice_auth_state_repository.
save_auth_state: an operator who has already confirmed a candidate's
identity out-of-band runs it directly.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from db.supabase_client import get_supabase_client

DEFAULT_TTL_MINUTES = 20


class BootstrapCodeInvalidError(RuntimeError):
    """Raised for a code that doesn't exist, is already consumed, or has
    expired -- deliberately one error type for all three so a caller
    (and the HTTP layer above it) can't distinguish which, and so can't
    be used to probe for valid-but-expired vs. never-existed codes."""


def _hash_code(raw_code: str) -> str:
    return hashlib.sha256(raw_code.encode("utf-8")).hexdigest()


def issue_bootstrap_code(candidate_id: str, ttl_minutes: int = DEFAULT_TTL_MINUTES) -> tuple[str, str]:
    """Operator-only. Returns (raw_code, expires_at_iso). The raw code is
    returned exactly once here and never stored -- only its hash is
    persisted, so a database read can never recover a usable code."""
    if not candidate_id:
        raise ValueError("candidate_id is required")

    raw_code = secrets.token_urlsafe(24)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)

    client = get_supabase_client()
    client.table("browser_bootstrap_codes").insert(
        {
            "code_hash": _hash_code(raw_code),
            "candidate_id": candidate_id,
            "expires_at": expires_at.isoformat(),
        }
    ).execute()

    return raw_code, expires_at.isoformat()


def consume_bootstrap_code(raw_code: str) -> str:
    """Atomically consumes a bootstrap code (via the
    consume_browser_bootstrap_code() Postgres function -- a single
    UPDATE ... WHERE still-eligible statement, safe under concurrent
    callers across multiple processes) and returns the candidate_id it
    was issued for. Raises BootstrapCodeInvalidError for anything else:
    unknown code, already consumed, or expired."""
    if not raw_code:
        raise BootstrapCodeInvalidError("no bootstrap code supplied")

    client = get_supabase_client()
    result = client.rpc(
        "consume_browser_bootstrap_code", {"p_code_hash": _hash_code(raw_code)}
    ).execute()
    rows = result.data or []
    if not rows:
        raise BootstrapCodeInvalidError("bootstrap code is invalid, already used, or expired")

    return rows[0]["candidate_id"]
