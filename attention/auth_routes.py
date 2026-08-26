"""Phase F2B (revised): the public HTTP surface for the browser trust
chain -- bootstrap code -> Telegram-approved challenge -> the existing
customer session token (db.customer_identity). Every response on this
blueprint is deliberately shaped to leak nothing about candidate
existence, Telegram-binding state, or internal ids: a bad bootstrap code,
an unbound-Telegram candidate, and any other bootstrap failure all
produce the exact same response.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from attention.channels import primary_channel_for_candidate
from attention.providers.telegram import TelegramProvider
from db.browser_bootstrap import BootstrapCodeInvalidError, consume_bootstrap_code
from db.browser_login_challenge import (
    ChallengeNotApprovableError,
    consume_challenge_for_exchange,
    create_challenge,
    expire_challenge,
    get_challenge,
    verify_challenge_secret,
)
from db.customer_identity import issue_session_token

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")

_BOOTSTRAP_UNAVAILABLE = ({"error": "bootstrap_unavailable"}, 403)


def _has_verified_telegram_binding(candidate_id: str) -> bool:
    primary = primary_channel_for_candidate(candidate_id)
    return bool(primary and primary["channel"] == "TELEGRAM" and primary.get("verified_at"))


def _challenge_secret_from_request() -> str:
    auth_header = request.headers.get("Authorization", "")
    return auth_header[len("Bootstrap ") :] if auth_header.startswith("Bootstrap ") else ""


@auth_bp.post("/bootstrap")
def start_bootstrap():
    body = request.get_json(silent=True) or {}
    code = body.get("code", "")

    try:
        candidate_id = consume_bootstrap_code(code)
    except BootstrapCodeInvalidError:
        return jsonify(_BOOTSTRAP_UNAVAILABLE[0]), _BOOTSTRAP_UNAVAILABLE[1]

    # A consumed bootstrap code is never un-consumed past this point, even
    # if the rest of this request fails -- a leaked/mistyped code being
    # single-use is more important than being forgiving. Any failure past
    # here means "issue a new bootstrap code", not "try the same one
    # again" (Phase 13).
    if not _has_verified_telegram_binding(candidate_id):
        return jsonify(_BOOTSTRAP_UNAVAILABLE[0]), _BOOTSTRAP_UNAVAILABLE[1]

    challenge_id, challenge_secret, expires_at = create_challenge(candidate_id)

    try:
        primary = primary_channel_for_candidate(candidate_id)
        chat_id = primary["external_user_id"]
        TelegramProvider(chat_id=chat_id).send_login_approval_request(challenge_id)
    except Exception:  # noqa: BLE001 - delivery failure must not leave an unapprovable PENDING challenge
        expire_challenge(challenge_id)
        return jsonify(_BOOTSTRAP_UNAVAILABLE[0]), _BOOTSTRAP_UNAVAILABLE[1]

    return jsonify(
        {
            "status": "PENDING",
            "challengeId": challenge_id,
            "challengeSecret": challenge_secret,
            "expiresInSeconds": 300,
        }
    )


@auth_bp.get("/bootstrap/status/<challenge_id>")
def bootstrap_status(challenge_id: str):
    secret = _challenge_secret_from_request()
    challenge = get_challenge(challenge_id)

    if challenge is None or not verify_challenge_secret(challenge, secret):
        # Same shape whether the id is unknown or the secret is wrong --
        # knowing challenge_id alone must never be enough to learn status.
        return jsonify({"error": "unauthorized"}), 401

    status = challenge["status"]
    if status == "CONSUMED":
        # Already exchanged -- from the browser's perspective this reads
        # the same as APPROVED (it got its token already); CONSUMED is an
        # internal-only state, never surfaced.
        status = "APPROVED"
    return jsonify({"status": status})


@auth_bp.post("/bootstrap/exchange/<challenge_id>")
def bootstrap_exchange(challenge_id: str):
    secret = _challenge_secret_from_request()
    challenge = get_challenge(challenge_id)

    if challenge is None or not verify_challenge_secret(challenge, secret):
        return jsonify({"error": "unauthorized"}), 401

    try:
        consumed = consume_challenge_for_exchange(challenge_id)
    except ChallengeNotApprovableError:
        return jsonify({"error": "not_approved"}), 409

    # candidate_id comes only from the consumed challenge row -- never
    # from anything the browser supplied in this request.
    token = issue_session_token(consumed["candidate_id"])
    return jsonify({"accessToken": token, "expiresInSeconds": 300})
