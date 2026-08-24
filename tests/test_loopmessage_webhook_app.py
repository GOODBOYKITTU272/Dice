"""Phase 7.11: the public Flask receiver itself -- auth header
enforcement and payload dispatch. Uses Flask's test client (no real
network); process_loopmessage_webhook's own dispatch logic is covered
in tests/test_attention_consumer.py, this file only proves the HTTP
layer around it.
"""
from __future__ import annotations

import attention.loopmessage_webhook_app as webhook_app


def _client():
    return webhook_app.app.test_client()


def test_healthz_returns_ok():
    resp = _client().get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"


def test_webhook_without_configured_secret_accepts_any_request(monkeypatch):
    monkeypatch.delenv("LOOPMESSAGE_WEBHOOK_SECRET", raising=False)
    monkeypatch.setattr(webhook_app, "process_loopmessage_webhook", lambda payload: "processed")

    resp = _client().post("/webhooks/loopmessage", json={"event": "message_inbound"})

    assert resp.status_code == 200
    assert resp.get_json()["result"] == "processed"


def test_webhook_rejects_missing_secret_when_configured(monkeypatch):
    monkeypatch.setenv("LOOPMESSAGE_WEBHOOK_SECRET", "real-secret")
    monkeypatch.setattr(webhook_app, "process_loopmessage_webhook", lambda payload: "processed")

    resp = _client().post("/webhooks/loopmessage", json={"event": "message_inbound"})

    assert resp.status_code == 401


def test_webhook_accepts_correct_secret_header(monkeypatch):
    monkeypatch.setenv("LOOPMESSAGE_WEBHOOK_SECRET", "real-secret")
    calls = []
    monkeypatch.setattr(webhook_app, "process_loopmessage_webhook", lambda payload: calls.append(payload) or "processed")

    resp = _client().post(
        "/webhooks/loopmessage",
        json={"event": "message_inbound", "contact": "+15551234567", "text": "APPLY", "message_id": "m-1"},
        headers={"X-Loop-Webhook-Secret": "real-secret"},
    )

    assert resp.status_code == 200
    assert calls == [{"event": "message_inbound", "contact": "+15551234567", "text": "APPLY", "message_id": "m-1"}]


def test_webhook_malformed_body_never_500s(monkeypatch):
    monkeypatch.delenv("LOOPMESSAGE_WEBHOOK_SECRET", raising=False)
    monkeypatch.setattr(webhook_app, "process_loopmessage_webhook", lambda payload: "ignored_no_op")

    resp = _client().post("/webhooks/loopmessage", data="not json", content_type="text/plain")

    assert resp.status_code == 200
