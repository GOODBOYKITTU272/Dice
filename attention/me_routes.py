"""Phase F2B: authenticated /me/* HTTP surface for the ApplyWizz frontend.

Every route resolves candidate_id from a Dice-owned signed session token
(db.customer_identity.verify_session_token) -- a request body or query
string can never supply candidate_id itself, so there is no way to read
or act on someone else's connections. This backend never talks to the
frontend's Supabase project or holds any of its credentials.

Registered onto the existing loopmessage-webhook Flask app rather than a
new Railway service (same "smallest safe mechanism" call as the
discovery daemon: reuse a deployed process instead of standing up new
infrastructure).
"""
from __future__ import annotations

from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

import readiness
from attention.channels import create_link_code
from db.customer_identity import InvalidTokenError, MissingSigningSecretError, verify_session_token
from db.supabase_client import get_supabase_client

me_bp = Blueprint("me", __name__, url_prefix="/me")

_LINK_TTL_MINUTES = 10


def _candidate_id_or_error() -> tuple[str, None] | tuple[None, tuple[dict, int]]:
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    try:
        return verify_session_token(token), None
    except InvalidTokenError:
        return None, ({"error": "unauthorized"}, 401)
    except MissingSigningSecretError as exc:
        return None, ({"error": f"server_misconfigured: {exc}"}, 500)


def _telegram_status(candidate_id: str) -> dict:
    client = get_supabase_client()
    bound = (
        client.table("candidate_attention_channels")
        .select("verified_at")
        .eq("candidate_id", candidate_id)
        .eq("channel", "TELEGRAM")
        .eq("is_enabled", True)
        .execute()
        .data
    )
    if bound and bound[0].get("verified_at"):
        return {"status": "CONNECTED"}

    pending = (
        client.table("attention_link_codes")
        .select("code, expires_at")
        .eq("candidate_id", candidate_id)
        .eq("channel", "TELEGRAM")
        .is_("consumed_at", "null")
        .gt("expires_at", datetime.now(timezone.utc).isoformat())
        .execute()
        .data
    )
    if pending:
        return {"status": "LINK_READY", "code": pending[0]["code"]}

    return {"status": "NOT_CONNECTED"}


_DICE_STATUS_BY_BLOCKER = {
    None: "CONNECTED",
    readiness.Blocker.AUTH_HEALTH_STALE: "CONNECTED",
    readiness.Blocker.AUTH_REQUIRED: "RECONNECT_REQUIRED",
    readiness.Blocker.AUTH_NEVER_VERIFIED: "NOT_CONNECTED",
}


def _dice_status(candidate_id: str) -> dict:
    check = readiness.check_dice_auth_ready(candidate_id)
    status = _DICE_STATUS_BY_BLOCKER.get(check.blocker, "NOT_CONNECTED")
    return {"status": status}


@me_bp.get("/connections/telegram")
def get_telegram_status():
    candidate_id, error = _candidate_id_or_error()
    if error:
        return jsonify(error[0]), error[1]
    return jsonify(_telegram_status(candidate_id))


@me_bp.post("/connections/telegram/link")
def create_telegram_link():
    candidate_id, error = _candidate_id_or_error()
    if error:
        return jsonify(error[0]), error[1]
    code = create_link_code(candidate_id, "TELEGRAM", ttl_minutes=_LINK_TTL_MINUTES)
    return jsonify(
        {
            "code": code,
            "expiresInMinutes": _LINK_TTL_MINUTES,
            "instructions": f"Send /start {code} to the ApplyWizz Telegram bot to finish connecting.",
        }
    )


@me_bp.get("/connections/dice")
def get_dice_status():
    candidate_id, error = _candidate_id_or_error()
    if error:
        return jsonify(error[0]), error[1]
    return jsonify(_dice_status(candidate_id))


@me_bp.post("/connections/dice")
def start_dice_connection():
    candidate_id, error = _candidate_id_or_error()
    if error:
        return jsonify(error[0]), error[1]
    # No interactive login transport exists yet (Browserless LiveURL needs a
    # paid plan we're not on -- confirmed live, not a guess). Reporting that
    # honestly instead of faking a connect flow.
    return jsonify({"outcome": "unavailable", "reason": "interactive_login_transport_not_available"}), 200


@me_bp.post("/connections/dice/reconnect")
def reconnect_dice_connection():
    candidate_id, error = _candidate_id_or_error()
    if error:
        return jsonify(error[0]), error[1]
    return jsonify({"outcome": "unavailable", "reason": "interactive_login_transport_not_available"}), 200


@me_bp.get("/oneclick/status")
def get_oneclick_status():
    candidate_id, error = _candidate_id_or_error()
    if error:
        return jsonify(error[0]), error[1]
    return jsonify(
        {
            "diceConnected": _dice_status(candidate_id)["status"] == "CONNECTED",
            "telegramConnected": _telegram_status(candidate_id)["status"] == "CONNECTED",
        }
    )
