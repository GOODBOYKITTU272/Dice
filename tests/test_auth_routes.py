"""Phase F2B (revised): the public /auth/bootstrap/* HTTP surface --
end-to-end trust chain (bootstrap code -> Telegram approval -> customer
session), through the real Flask app. Every response-shape/leak
assertion here matters as much as the happy path.
"""
from __future__ import annotations

import threading
from unittest.mock import patch

import attention.loopmessage_webhook_app as webhook_app
from attention.channels import bind_channel
from db.browser_bootstrap import issue_bootstrap_code
from db.browser_login_challenge import approve_challenge, create_challenge, get_challenge, verify_challenge_secret
from db.customer_identity import verify_session_token


def _client():
    return webhook_app.app.test_client()


def _bind_verified_telegram(candidate_id: str, chat_id: str) -> None:
    bind_channel(candidate_id, "TELEGRAM", chat_id, verified=True)


# --- POST /auth/bootstrap ---


def test_bootstrap_ignores_candidate_id_in_request_body(fake_auth_client, dice_session_secret):
    _bind_verified_telegram("cand-1", "111")
    raw_code, _exp = issue_bootstrap_code("cand-1")

    with patch("attention.auth_routes.TelegramProvider"):
        resp = _client().post(
            "/auth/bootstrap", json={"code": raw_code, "candidate_id": "attacker-supplied"}
        )

    assert resp.status_code == 200
    # The only way to check which candidate this resolved to is via the
    # challenge row itself (never exposed in the response) -- confirming
    # here that it's cand-1 (from the code), not the attacker-supplied one.
    challenge_id = resp.get_json()["challengeId"]
    assert get_challenge(challenge_id)["candidate_id"] == "cand-1"


def test_invalid_code_and_valid_code_without_telegram_look_identical(fake_auth_client, dice_session_secret):
    bad_code_resp = _client().post("/auth/bootstrap", json={"code": "never-issued"})

    raw_code, _exp = issue_bootstrap_code("cand-no-telegram")  # no verified binding
    no_telegram_resp = _client().post("/auth/bootstrap", json={"code": raw_code})

    assert bad_code_resp.status_code == no_telegram_resp.status_code == 403
    assert bad_code_resp.get_json() == no_telegram_resp.get_json()


def test_bootstrap_response_never_exposes_candidate_id(fake_auth_client, dice_session_secret):
    _bind_verified_telegram("cand-1", "111")
    raw_code, _exp = issue_bootstrap_code("cand-1")

    with patch("attention.auth_routes.TelegramProvider"):
        resp = _client().post("/auth/bootstrap", json={"code": raw_code})

    assert "cand-1" not in resp.get_data(as_text=True)


def test_bootstrap_sends_telegram_approval_only_to_the_bound_chat(fake_auth_client, dice_session_secret):
    _bind_verified_telegram("cand-1", "111")
    raw_code, _exp = issue_bootstrap_code("cand-1")

    with patch("attention.auth_routes.TelegramProvider") as mock_provider_cls:
        _client().post("/auth/bootstrap", json={"code": raw_code})

    mock_provider_cls.assert_called_once_with(chat_id="111")
    mock_provider_cls.return_value.send_login_approval_request.assert_called_once()


def test_telegram_send_failure_leaves_challenge_expired_not_stuck_pending(fake_auth_client, dice_session_secret):
    _bind_verified_telegram("cand-1", "111")
    raw_code, _exp = issue_bootstrap_code("cand-1")

    with patch("attention.auth_routes.TelegramProvider") as mock_provider_cls:
        mock_provider_cls.return_value.send_login_approval_request.side_effect = RuntimeError("network down")
        resp = _client().post("/auth/bootstrap", json={"code": raw_code})

    assert resp.status_code == 403
    # The consumed bootstrap code is never un-consumed -- confirm it can't be retried.
    with patch("attention.auth_routes.TelegramProvider"):
        retry = _client().post("/auth/bootstrap", json={"code": raw_code})
    assert retry.status_code == 403


# --- GET /auth/bootstrap/status/<id> ---


def test_status_requires_both_id_and_secret(fake_auth_client):
    challenge_id, secret, _exp = create_challenge("cand-1")

    no_secret = _client().get(f"/auth/bootstrap/status/{challenge_id}")
    assert no_secret.status_code == 401

    wrong_secret = _client().get(
        f"/auth/bootstrap/status/{challenge_id}", headers={"Authorization": "Bootstrap wrong"}
    )
    assert wrong_secret.status_code == 401

    correct = _client().get(
        f"/auth/bootstrap/status/{challenge_id}", headers={"Authorization": f"Bootstrap {secret}"}
    )
    assert correct.status_code == 200
    assert correct.get_json()["status"] == "PENDING"


def test_status_response_never_exposes_candidate_id(fake_auth_client):
    challenge_id, secret, _exp = create_challenge("cand-1")
    resp = _client().get(
        f"/auth/bootstrap/status/{challenge_id}", headers={"Authorization": f"Bootstrap {secret}"}
    )
    assert "cand-1" not in resp.get_data(as_text=True)


def test_denied_challenge_reports_denied_status(fake_auth_client):
    from db.browser_login_challenge import deny_challenge

    challenge_id, secret, _exp = create_challenge("cand-1")
    deny_challenge(challenge_id)
    resp = _client().get(
        f"/auth/bootstrap/status/{challenge_id}", headers={"Authorization": f"Bootstrap {secret}"}
    )
    assert resp.get_json()["status"] == "DENIED"


# --- POST /auth/bootstrap/exchange/<id> ---


def test_pending_challenge_cannot_exchange(fake_auth_client):
    challenge_id, secret, _exp = create_challenge("cand-1")
    resp = _client().post(
        f"/auth/bootstrap/exchange/{challenge_id}", headers={"Authorization": f"Bootstrap {secret}"}
    )
    assert resp.status_code == 409


def test_denied_challenge_cannot_exchange(fake_auth_client):
    from db.browser_login_challenge import deny_challenge

    challenge_id, secret, _exp = create_challenge("cand-1")
    deny_challenge(challenge_id)
    resp = _client().post(
        f"/auth/bootstrap/exchange/{challenge_id}", headers={"Authorization": f"Bootstrap {secret}"}
    )
    assert resp.status_code == 409


def test_approved_challenge_exchanges_for_a_working_session_token(fake_auth_client, dice_session_secret):
    challenge_id, secret, _exp = create_challenge("cand-1")
    approve_challenge(challenge_id)

    resp = _client().post(
        f"/auth/bootstrap/exchange/{challenge_id}", headers={"Authorization": f"Bootstrap {secret}"}
    )

    assert resp.status_code == 200
    token = resp.get_json()["accessToken"]
    assert verify_session_token(token) == "cand-1"


def test_exchange_replay_is_rejected(fake_auth_client, dice_session_secret):
    challenge_id, secret, _exp = create_challenge("cand-1")
    approve_challenge(challenge_id)

    first = _client().post(
        f"/auth/bootstrap/exchange/{challenge_id}", headers={"Authorization": f"Bootstrap {secret}"}
    )
    second = _client().post(
        f"/auth/bootstrap/exchange/{challenge_id}", headers={"Authorization": f"Bootstrap {secret}"}
    )

    assert first.status_code == 200
    assert second.status_code == 409


def test_exchanged_token_authenticates_me_routes(fake_auth_client, dice_session_secret, monkeypatch):
    import attention.me_routes as me_routes

    monkeypatch.setattr(me_routes, "_dice_status", lambda candidate_id: {"status": "NOT_CONNECTED"})
    monkeypatch.setattr(me_routes, "_telegram_status", lambda candidate_id: {"status": "CONNECTED"})

    challenge_id, secret, _exp = create_challenge("cand-1")
    approve_challenge(challenge_id)
    exchange_resp = _client().post(
        f"/auth/bootstrap/exchange/{challenge_id}", headers={"Authorization": f"Bootstrap {secret}"}
    )
    token = exchange_resp.get_json()["accessToken"]

    me_resp = _client().get("/me/oneclick/status", headers={"Authorization": f"Bearer {token}"})

    assert me_resp.status_code == 200
    assert me_resp.get_json() == {"diceConnected": False, "telegramConnected": True}


def test_concurrent_exchange_issues_exactly_one_session(fake_auth_client, dice_session_secret):
    challenge_id, secret, _exp = create_challenge("cand-1")
    approve_challenge(challenge_id)

    statuses: list[int] = [None, None]

    def _attempt(i):
        resp = _client().post(
            f"/auth/bootstrap/exchange/{challenge_id}", headers={"Authorization": f"Bootstrap {secret}"}
        )
        statuses[i] = resp.status_code

    threads = [threading.Thread(target=_attempt, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(statuses) == [200, 409]
