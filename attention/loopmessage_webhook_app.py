"""Phase 7.11: public HTTP receiver for LoopMessage's inbound iMessage
webhook. Deployed as its own small always-on service -- LoopMessage
needs a real, publicly reachable URL to POST inbound replies to, so
this can't be the same long-polling model attention/consumer.py uses
for Telegram.

Verification is a shared secret configured both here
(LOOPMESSAGE_WEBHOOK_SECRET) and as the "Webhook header" value in
LoopMessage's own dashboard. Real bug, live-found 2026-08-24: their own
dashboard field is explicit -- "Will send an HTTP Authorization header
with this value" -- but this module originally checked a custom header
name (X-Loop-Webhook-Secret) that LoopMessage never sends, so every
real webhook call was silently 401ing here while our own diagnostic
(railway logs) was separately showing stale/cached output, making the
failure look like it was on LoopMessage's end. The header LoopMessage
actually sends is the bare secret value in `Authorization` -- no
"Bearer" prefix, matching how their own send-message API takes the
API key directly in that same header. If the secret isn't configured
here, the header check is skipped (dev-convenience) -- never enforced
silently wrong, but also never blocking local/offline testing.
"""
from __future__ import annotations

import os

from flask import Flask, request

from attention.consumer import process_loopmessage_webhook

app = Flask(__name__)

_SECRET_ENV_VAR = "LOOPMESSAGE_WEBHOOK_SECRET"
_SECRET_HEADER = "Authorization"


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
