"""Phase M8B: candidate-scoped Dice auth state, backed by Supabase Vault
(supabase/migrations/20260825060000_candidate_scoped_dice_auth_state.sql).
Raw cookie JSON never touches this module or any log line -- it only
ever crosses the wire inside these three RPC calls, decrypted server-
side by a SECURITY DEFINER function granted to service_role alone.
"""
from __future__ import annotations

from db.supabase_client import get_supabase_client


def save_auth_state(candidate_id: str, cookies_json: str, provisioned_by: str = "operator_manual") -> str:
    """Creates or replaces (on reconnect) the one auth-state record for
    this candidate. Never affects any other candidate's row -- enforced
    by the DB's own unique(candidate_id) constraint, not just this call
    passing the right id."""
    client = get_supabase_client()
    result = client.rpc(
        "dice_auth_state_set",
        {"p_candidate_id": candidate_id, "p_cookies_json": cookies_json, "p_provisioned_by": provisioned_by},
    ).execute()
    return result.data


def get_auth_state(candidate_id: str) -> str | None:
    """Returns this candidate's decrypted cookie JSON, or None if no
    ACTIVE state exists. Callers must treat None as AUTH_REQUIRED --
    never fall back to a different candidate's state or a global one."""
    client = get_supabase_client()
    result = client.rpc("dice_auth_state_get", {"p_candidate_id": candidate_id}).execute()
    return result.data


def invalidate_auth_state(candidate_id: str, reason: str) -> None:
    client = get_supabase_client()
    client.rpc("dice_auth_state_invalidate", {"p_candidate_id": candidate_id, "p_reason": reason}).execute()
