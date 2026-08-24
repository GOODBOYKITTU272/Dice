"""Phase 7.11: public HTTP receiver for LoopMessage's inbound iMessage
webhook. Deployed as its own small always-on service -- LoopMessage
needs a real, publicly reachable URL to POST inbound replies to, so
this can't be the same long-polling model attention/consumer.py uses
for Telegram.

Verification is a shared secret configured both here
(LOOPMESSAGE_WEBHOOK_SECRET) and as a custom header on the webhook in
LoopMessage's own dashboard -- their webhook system offers no
cryptographic signature, only "the request contains the configured
authorization header you set up" (their docs, 2026-08-24), so a
matching header is the whole auth mechanism. If the secret isn't
configured here, the header check is skipped (matches this project's
existing dev-convenience pattern elsewhere) -- never enforced silently
wrong, but also never blocking local/offline testing.
"""
from __future__ import annotations

import os

from flask import Flask, request

from attention.consumer import process_loopmessage_webhook

app = Flask(__name__)

_SECRET_ENV_VAR = "LOOPMESSAGE_WEBHOOK_SECRET"
_SECRET_HEADER = "X-Loop-Webhook-Secret"


@app.get("/healthz")
def healthz():
    return {"status": "ok"}, 200


@app.post("/webhooks/loopmessage")
def loopmessage_webhook():
    configured_secret = os.environ.get(_SECRET_ENV_VAR)
    if configured_secret and request.headers.get(_SECRET_HEADER) != configured_secret:
        return {"error": "unauthorized"}, 401

    payload = request.get_json(silent=True) or {}
    result = process_loopmessage_webhook(payload)
    return {"result": result}, 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
