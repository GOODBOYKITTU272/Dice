"""Phase F2B (revised): a Dice-owned, short-lived signed session token for
authenticated /me/* requests -- replacing the earlier frontend-Supabase-JWT
design (db/frontend_identity.py, removed) so Dice never depends on the
ApplyWizz frontend's Supabase project, credentials, or auth internals.

Verification only trusts a token this module itself signed: HMAC-SHA256
over a small claims payload (candidate_id, issuer, audience, issued-at,
expiry, jti), using DICE_CUSTOMER_SESSION_SECRET -- a value that exists
only on this backend, generated once and never sent to a browser. This
mirrors the existing LOOPMESSAGE_WEBHOOK_SECRET shared-secret convention
in attention/loopmessage_webhook_app.py rather than inventing an
unrelated pattern.

Deliberate scope limit: this module exposes `issue_session_token`, but
nothing in attention/me_routes.py (or anywhere else reachable over HTTP)
calls it. There is currently no verified channel inside this repo through
which an unknown browser can prove "I am candidate X" for the first time
-- every candidate_id that exists today got there through an operator
manually provisioning it (see STATE.md's M8B note: "an operator provisions
each candidate's cookies manually via db.dice_auth_state_repository.
save_auth_state(candidate_id, cookies_json)"). issue_session_token exists
for that same already-established operator-trust model: a human who has
already confirmed a candidate's identity out-of-band mints them a token
directly (e.g. a one-off `python -c` invocation), the same way cookies are
provisioned today. Building an HTTP endpoint that accepts a candidate_id
and returns a token would look secure while actually accepting identity
from an untrusted caller -- deliberately not done. Self-service issuance
for a brand-new customer remains an open product question, not something
this module papers over.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import uuid

ISSUER = "applywizz-dice"
AUDIENCE = "dice-me-routes"
DEFAULT_TTL_SECONDS = 300  # 5 minutes, the max this repo's task allows

_SECRET_ENV_VAR = "DICE_CUSTOMER_SESSION_SECRET"


class MissingSigningSecretError(RuntimeError):
    pass


class InvalidTokenError(RuntimeError):
    """Raised for a missing, malformed, expired, or wrong-signature/
    issuer/audience token. Deliberately one error type for every one of
    these -- an attacker probing for which check failed learns nothing
    from the response, only ever a flat 401."""


def _signing_secret() -> str:
    secret = os.environ.get(_SECRET_ENV_VAR)
    if not secret:
        raise MissingSigningSecretError(f"{_SECRET_ENV_VAR} is not configured")
    return secret


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _sign(payload_b64: str, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return _b64encode(digest)


def issue_session_token(candidate_id: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> str:
    """Operator-only: mint a token for a candidate_id already confirmed
    through some other trusted channel. Never call this from an HTTP
    route handler -- see module docstring."""
    if not candidate_id:
        raise ValueError("candidate_id is required")

    now = int(time.time())
    payload = {
        "candidate_id": candidate_id,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + ttl_seconds,
        "jti": uuid.uuid4().hex,
    }
    payload_b64 = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature_b64 = _sign(payload_b64, _signing_secret())
    return f"{payload_b64}.{signature_b64}"


def verify_session_token(token: str) -> str:
    """The one function every /me/* route calls before doing anything
    else. Returns the verified candidate_id, or raises InvalidTokenError
    -- never partially trusts a token that fails any single check."""
    if not token:
        raise InvalidTokenError("no bearer token supplied")

    parts = token.split(".")
    if len(parts) != 2:
        raise InvalidTokenError("malformed token")
    payload_b64, signature_b64 = parts

    secret = _signing_secret()
    expected_signature_b64 = _sign(payload_b64, secret)
    if not hmac.compare_digest(signature_b64, expected_signature_b64):
        raise InvalidTokenError("signature mismatch")

    try:
        payload = json.loads(_b64decode(payload_b64))
    except Exception as exc:  # noqa: BLE001 - any decode failure is just an invalid token
        raise InvalidTokenError("malformed token payload") from exc

    if payload.get("iss") != ISSUER:
        raise InvalidTokenError("wrong issuer")
    if payload.get("aud") != AUDIENCE:
        raise InvalidTokenError("wrong audience")

    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or time.time() >= exp:
        raise InvalidTokenError("token expired")

    candidate_id = payload.get("candidate_id")
    if not candidate_id or not isinstance(candidate_id, str):
        raise InvalidTokenError("token missing candidate_id")

    return candidate_id
