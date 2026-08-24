"""Phase 7.11: LoopMessageProvider -- fully offline. requests.post is
monkeypatched with a fake response object; parse_inbound() itself never
makes a network call. Never sends a real iMessage to run.
"""
from __future__ import annotations

import pytest

import attention.providers.loopmessage as loopmessage_module
from attention.models import AttentionAction
from attention.providers.loopmessage import LoopMessageProvider


class _FakeResponse:
    def __init__(self, json_body, status_code=200):
        self._json_body = json_body
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json_body


def _capture_post(calls, message_id="msg-1"):
    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return _FakeResponse({"message_id": message_id, "success": True})

    return fake_post


def test_parse_inbound_apply_text_reply():
    provider = LoopMessageProvider()
    event = provider.parse_inbound({"message_id": "m-1", "text": "APPLY", "event": "message_inbound"})
    assert event.action == AttentionAction.APPLY
    assert event.external_message_id == "m-1"
    assert event.channel == "IMESSAGE"


def test_parse_inbound_apply_is_case_insensitive():
    provider = LoopMessageProvider()
    event = provider.parse_inbound({"message_id": "m-2", "text": "apply"})
    assert event.action == AttentionAction.APPLY


def test_parse_inbound_skip_text_reply():
    provider = LoopMessageProvider()
    event = provider.parse_inbound({"message_id": "m-3", "text": "  skip  "})
    assert event.action == AttentionAction.SKIP


def test_parse_inbound_confirm_and_edit_text_replies():
    provider = LoopMessageProvider()
    assert provider.parse_inbound({"message_id": "m-4", "text": "CONFIRM"}).action == AttentionAction.CONFIRM
    assert provider.parse_inbound({"message_id": "m-5", "text": "EDIT"}).action == AttentionAction.EDIT


# Live-found 2026-08-24: LoopMessage's own pipeline misclassifies short
# bare-word replies ("APPLY", "SKIP", ...) as OTP codes and silently
# drops them before our webhook ever sees them (confirmed by their
# support). Natural phrases containing the keyword must still resolve
# correctly -- these are the actual reply style send_job_offer/
# send_answer_confirmation now ask for.
def test_parse_inbound_natural_phrase_apply():
    provider = LoopMessageProvider()
    event = provider.parse_inbound({"message_id": "m-7", "text": "yes apply"})
    assert event.action == AttentionAction.APPLY


def test_parse_inbound_natural_phrase_skip():
    provider = LoopMessageProvider()
    event = provider.parse_inbound({"message_id": "m-8", "text": "no thanks, skip this one"})
    assert event.action == AttentionAction.SKIP


def test_parse_inbound_natural_phrase_confirm_and_edit():
    provider = LoopMessageProvider()
    assert provider.parse_inbound({"message_id": "m-9", "text": "yes confirm"}).action == AttentionAction.CONFIRM
    assert provider.parse_inbound({"message_id": "m-10", "text": "no edit please"}).action == AttentionAction.EDIT


def test_parse_inbound_free_text_falls_back_to_answer():
    provider = LoopMessageProvider()
    event = provider.parse_inbound({"message_id": "m-6", "text": "West Haven, CT"})
    assert event.action == AttentionAction.ANSWER
    assert event.raw_text == "West Haven, CT"


def test_send_job_offer_posts_to_loopmessage_api(monkeypatch):
    calls = []
    monkeypatch.setenv("LOOPMESSAGE_AUTH_KEY", "test-key")
    monkeypatch.setattr(loopmessage_module.requests, "post", _capture_post(calls))

    provider = LoopMessageProvider(contact="+15551234567")
    message_id = provider.send_job_offer({"id": "app-1"}, {"title": "Java Developer", "company_name": "ABC Corp"})

    assert message_id == "msg-1"
    assert len(calls) == 1
    call = calls[0]
    assert call["url"] == "https://a.loopmessage.com/api/v1/message/send/"
    assert call["headers"]["Authorization"] == "test-key"
    assert call["json"]["contact"] == "+15551234567"
    assert "Java Developer" in call["json"]["text"]
    assert 'Reply "yes apply" to apply, or "no skip" to skip' in call["json"]["text"]
    assert "sender" not in call["json"]  # no LOOPMESSAGE_SENDER_NAME configured -- omitted, not sent empty


def test_send_includes_sender_when_configured(monkeypatch):
    calls = []
    monkeypatch.setenv("LOOPMESSAGE_AUTH_KEY", "test-key")
    monkeypatch.setenv("LOOPMESSAGE_SENDER_NAME", "sender-abc-123")
    monkeypatch.setattr(loopmessage_module.requests, "post", _capture_post(calls))

    LoopMessageProvider(contact="+15551234567").send_apply_ack("app-1")

    assert calls[0]["json"]["sender"] == "sender-abc-123"


def test_send_apply_ack_and_skip_ack_and_answer_accepted_and_ready_to_submit(monkeypatch):
    calls = []
    monkeypatch.setenv("LOOPMESSAGE_AUTH_KEY", "test-key")
    monkeypatch.setattr(loopmessage_module.requests, "post", _capture_post(calls))

    provider = LoopMessageProvider(contact="+15551234567")
    provider.send_apply_ack("app-1")
    provider.send_skip_ack("app-1")
    provider.send_answer_accepted("app-1", "q-1")
    provider.send_ready_to_submit("app-1")

    texts = [c["json"]["text"] for c in calls]
    assert "Checking" in texts[0]
    assert "Skipped" in texts[1]
    assert "Got it" in texts[2]
    assert "Submitting" in texts[3]


def test_send_without_configured_contact_raises(monkeypatch):
    monkeypatch.setenv("LOOPMESSAGE_AUTH_KEY", "test-key")
    monkeypatch.delenv("LOOPMESSAGE_CONTACT", raising=False)
    provider = LoopMessageProvider()
    with pytest.raises(loopmessage_module.LoopMessageNotConfiguredError):
        provider.send_apply_ack("app-1")


def test_send_without_auth_key_raises(monkeypatch):
    monkeypatch.delenv("LOOPMESSAGE_AUTH_KEY", raising=False)
    provider = LoopMessageProvider(contact="+15551234567")
    with pytest.raises(loopmessage_module.LoopMessageNotConfiguredError):
        provider.send_apply_ack("app-1")
