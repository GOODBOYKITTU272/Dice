"""Phase F2B (revised): the authenticated /me/* HTTP layer. Every route
must resolve candidate_id from a Dice-owned signed session token and
MUST NOT be influenceable by a candidate_id supplied in the request
itself -- that is the actual security property this bridge exists to
guarantee, so the negative tests below are the load-bearing ones, not
the happy path.
"""
from __future__ import annotations

import time

import attention.me_routes as me_routes
import attention.loopmessage_webhook_app as webhook_app
from db.customer_identity import InvalidTokenError, issue_session_token


def _client():
    return webhook_app.app.test_client()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --- token rejection paths (route-level, on top of test_customer_identity.py's unit coverage) ---


def test_missing_bearer_token_is_401(monkeypatch):
    def _boom(token):
        raise InvalidTokenError("no token")

    monkeypatch.setattr(me_routes, "verify_session_token", _boom)

    resp = _client().get("/me/connections/dice")

    assert resp.status_code == 401


def test_invalid_or_expired_token_is_401_not_500(monkeypatch):
    def _boom(token):
        raise InvalidTokenError("token expired")

    monkeypatch.setattr(me_routes, "verify_session_token", _boom)

    resp = _client().get("/me/connections/dice", headers=_auth("garbage.token"))

    assert resp.status_code == 401


def test_malformed_token_is_401(dice_session_secret):
    resp = _client().get("/me/connections/dice", headers=_auth("not-a-real-token"))
    assert resp.status_code == 401


def test_expired_token_is_401_at_route_level(dice_session_secret, monkeypatch):
    token = issue_session_token("cand-1", ttl_seconds=1)
    original_time = time.time
    monkeypatch.setattr(time, "time", lambda: original_time() + 10)

    resp = _client().get("/me/connections/dice", headers=_auth(token))

    assert resp.status_code == 401


def test_wrong_issuer_token_is_401_at_route_level(dice_session_secret, monkeypatch):
    import db.customer_identity as customer_identity

    monkeypatch.setattr(customer_identity, "ISSUER", "someone-else")
    token = issue_session_token("cand-1")
    monkeypatch.setattr(customer_identity, "ISSUER", "applywizz-dice")

    resp = _client().get("/me/connections/dice", headers=_auth(token))

    assert resp.status_code == 401


def test_wrong_audience_token_is_401_at_route_level(dice_session_secret, monkeypatch):
    import db.customer_identity as customer_identity

    monkeypatch.setattr(customer_identity, "AUDIENCE", "someone-elses-service")
    token = issue_session_token("cand-1")
    monkeypatch.setattr(customer_identity, "AUDIENCE", "dice-me-routes")

    resp = _client().get("/me/connections/dice", headers=_auth(token))

    assert resp.status_code == 401


# --- candidate_id can only ever come from the verified token ---


def test_candidate_id_in_request_body_cannot_override_identity(monkeypatch):
    seen = {}

    monkeypatch.setattr(me_routes, "verify_session_token", lambda token: "real-candidate-from-token")

    def _fake_create_link_code(candidate_id, channel, ttl_minutes=10):
        seen["candidate_id"] = candidate_id
        return "ABCD1234"

    monkeypatch.setattr(me_routes, "create_link_code", _fake_create_link_code)

    resp = _client().post(
        "/me/connections/telegram/link",
        headers=_auth("real.token.here"),
        json={"candidate_id": "attacker-supplied-other-candidate"},
    )

    assert resp.status_code == 200
    assert seen["candidate_id"] == "real-candidate-from-token"


def test_candidate_id_in_query_string_cannot_override_identity(monkeypatch):
    monkeypatch.setattr(me_routes, "verify_session_token", lambda token: "real-candidate-from-token")
    monkeypatch.setattr(me_routes, "_dice_status", lambda candidate_id: {"status": candidate_id})

    resp = _client().get(
        "/me/connections/dice?candidate_id=attacker-supplied-other-candidate",
        headers=_auth("real.token.here"),
    )

    assert resp.status_code == 200
    assert resp.get_json()["status"] == "real-candidate-from-token"


def test_candidate_a_cannot_read_candidate_bs_telegram_state(dice_session_secret, monkeypatch):
    seen_candidate_ids = []

    def _fake_telegram_status(candidate_id):
        seen_candidate_ids.append(candidate_id)
        return {"status": "CONNECTED" if candidate_id == "candidate-b" else "NOT_CONNECTED"}

    monkeypatch.setattr(me_routes, "_telegram_status", _fake_telegram_status)

    token_for_a = issue_session_token("candidate-a")
    resp = _client().get("/me/connections/telegram", headers=_auth(token_for_a))

    assert resp.status_code == 200
    assert resp.get_json()["status"] == "NOT_CONNECTED"
    assert seen_candidate_ids == ["candidate-a"]


def test_candidate_a_cannot_read_candidate_bs_dice_state(dice_session_secret, monkeypatch):
    seen_candidate_ids = []

    def _fake_dice_status(candidate_id):
        seen_candidate_ids.append(candidate_id)
        return {"status": "CONNECTED" if candidate_id == "candidate-b" else "NOT_CONNECTED"}

    monkeypatch.setattr(me_routes, "_dice_status", _fake_dice_status)

    token_for_a = issue_session_token("candidate-a")
    resp = _client().get("/me/connections/dice", headers=_auth(token_for_a))

    assert resp.status_code == 200
    assert resp.get_json()["status"] == "NOT_CONNECTED"
    assert seen_candidate_ids == ["candidate-a"]


# --- canonical routes ---


def test_get_telegram_status_uses_the_canonical_route_not_the_link_path(monkeypatch):
    monkeypatch.setattr(me_routes, "verify_session_token", lambda token: "cand-1")
    monkeypatch.setattr(me_routes, "_telegram_status", lambda candidate_id: {"status": "NOT_CONNECTED"})

    resp = _client().get("/me/connections/telegram", headers=_auth("real.token.here"))

    assert resp.status_code == 200
    assert resp.get_json()["status"] == "NOT_CONNECTED"


def test_old_link_path_no_longer_serves_status(monkeypatch):
    """The bug this replaces: GET .../connections/telegram/link used to
    BE the status route. Now only POST lives there."""
    monkeypatch.setattr(me_routes, "verify_session_token", lambda token: "cand-1")

    resp = _client().get("/me/connections/telegram/link", headers=_auth("real.token.here"))

    assert resp.status_code == 405  # method not allowed: only POST is registered here


def test_telegram_link_code_created_for_exact_authenticated_candidate(monkeypatch):
    seen = {}

    monkeypatch.setattr(me_routes, "verify_session_token", lambda token: "cand-1")

    def _fake_create_link_code(candidate_id, channel, ttl_minutes=10):
        seen["candidate_id"] = candidate_id
        seen["channel"] = channel
        return "ABCD1234"

    monkeypatch.setattr(me_routes, "create_link_code", _fake_create_link_code)

    resp = _client().post("/me/connections/telegram/link", headers=_auth("real.token.here"))

    assert resp.status_code == 200
    assert seen == {"candidate_id": "cand-1", "channel": "TELEGRAM"}
    assert resp.get_json()["code"] == "ABCD1234"


def test_telegram_status_connected_reflects_real_canonical_binding(monkeypatch):
    monkeypatch.setattr(me_routes, "verify_session_token", lambda token: "cand-1")
    monkeypatch.setattr(me_routes, "_telegram_status", lambda candidate_id: {"status": "CONNECTED"})

    resp = _client().get("/me/connections/telegram", headers=_auth("real.token.here"))

    assert resp.status_code == 200
    assert resp.get_json()["status"] == "CONNECTED"


# --- Dice status/connect ---


def test_dice_status_maps_auth_health_stale_to_connected(monkeypatch):
    """M8C's self-recovery philosophy: staleness is an internal
    freshness concern, never surfaced to the customer as broken."""
    import readiness

    monkeypatch.setattr(me_routes, "verify_session_token", lambda token: "cand-1")
    monkeypatch.setattr(
        readiness,
        "check_dice_auth_ready",
        lambda candidate_id: readiness.CheckResult(False, "stale", readiness.Blocker.AUTH_HEALTH_STALE),
    )

    resp = _client().get("/me/connections/dice", headers=_auth("real.token.here"))

    assert resp.status_code == 200
    assert resp.get_json()["status"] == "CONNECTED"


def test_dice_connect_never_fakes_success(monkeypatch):
    monkeypatch.setattr(me_routes, "verify_session_token", lambda token: "cand-1")

    resp = _client().post("/me/connections/dice", headers=_auth("real.token.here"))

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["outcome"] == "unavailable"


def test_dice_reconnect_never_fakes_success(monkeypatch):
    monkeypatch.setattr(me_routes, "verify_session_token", lambda token: "cand-1")

    resp = _client().post("/me/connections/dice/reconnect", headers=_auth("real.token.here"))

    assert resp.status_code == 200
    assert resp.get_json()["outcome"] == "unavailable"


# --- oneclick status ---


def test_oneclick_status_never_reports_connected_without_real_check(monkeypatch):
    monkeypatch.setattr(me_routes, "verify_session_token", lambda token: "cand-1")
    monkeypatch.setattr(me_routes, "_dice_status", lambda candidate_id: {"status": "NOT_CONNECTED"})
    monkeypatch.setattr(me_routes, "_telegram_status", lambda candidate_id: {"status": "NOT_CONNECTED"})

    resp = _client().get("/me/oneclick/status", headers=_auth("real.token.here"))

    assert resp.status_code == 200
    assert resp.get_json() == {"diceConnected": False, "telegramConnected": False}


def test_oneclick_status_aggregates_real_candidate_scoped_states(monkeypatch):
    monkeypatch.setattr(me_routes, "verify_session_token", lambda token: "cand-1")
    monkeypatch.setattr(me_routes, "_dice_status", lambda candidate_id: {"status": "CONNECTED"})
    monkeypatch.setattr(me_routes, "_telegram_status", lambda candidate_id: {"status": "CONNECTED"})

    resp = _client().get("/me/oneclick/status", headers=_auth("real.token.here"))

    assert resp.status_code == 200
    assert resp.get_json() == {"diceConnected": True, "telegramConnected": True}


# --- secret hygiene ---


def test_signing_secret_never_appears_in_any_response_body(dice_session_secret, monkeypatch):
    monkeypatch.setattr(me_routes, "_dice_status", lambda candidate_id: {"status": "NOT_CONNECTED"})
    monkeypatch.setattr(me_routes, "_telegram_status", lambda candidate_id: {"status": "NOT_CONNECTED"})

    token = issue_session_token("cand-1")
    resp = _client().get("/me/oneclick/status", headers=_auth(token))

    assert dice_session_secret not in resp.get_data(as_text=True)
