"""Phase 7.4: TelegramProvider -- fully offline (requests.post mocked).
Never hits the real Telegram API, never needs a real bot token."""
from __future__ import annotations

import requests

from attention.models import AttentionAction
from attention.providers.telegram import TelegramProvider


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _capture_post(calls):
    def _post(url, json, timeout):
        calls.append((url, json))
        return _FakeResponse({"result": {"message_id": len(calls)}})

    return _post


# 19. Telegram Apply action normalizes correctly
def test_parse_inbound_apply_callback():
    provider = TelegramProvider()
    event = provider.parse_inbound({"update_id": 1, "callback_query": {"data": "APPLY"}})
    assert event.action == AttentionAction.APPLY
    assert event.external_message_id == "1"


def test_parse_inbound_skip_callback():
    provider = TelegramProvider()
    event = provider.parse_inbound({"update_id": 2, "callback_query": {"data": "SKIP"}})
    assert event.action == AttentionAction.SKIP


# 21. Telegram Confirm/Edit normalize correctly
def test_parse_inbound_confirm_and_edit_callbacks():
    provider = TelegramProvider()
    assert provider.parse_inbound({"update_id": 3, "callback_query": {"data": "CONFIRM"}}).action == AttentionAction.CONFIRM
    assert provider.parse_inbound({"update_id": 4, "callback_query": {"data": "EDIT"}}).action == AttentionAction.EDIT


def test_parse_inbound_answer_callback_carries_raw_text():
    provider = TelegramProvider()
    event = provider.parse_inbound({"update_id": 5, "callback_query": {"data": "ANSWER:Yes"}})
    assert event.action == AttentionAction.ANSWER
    assert event.raw_text == "Yes"


def test_parse_inbound_plain_text_reply_falls_back_to_answer():
    provider = TelegramProvider()
    event = provider.parse_inbound({"update_id": 6, "message": {"text": "West Haven, CT"}})
    assert event.action == AttentionAction.ANSWER
    assert event.raw_text == "West Haven, CT"


def test_parse_inbound_plain_text_apply_reply_normalizes_like_a_button():
    provider = TelegramProvider()
    event = provider.parse_inbound({"update_id": 7, "message": {"text": "apply"}})
    assert event.action == AttentionAction.APPLY


def test_parse_inbound_external_message_id_is_the_update_id():
    provider = TelegramProvider()
    event = provider.parse_inbound({"update_id": 999, "callback_query": {"data": "SKIP"}})
    assert event.external_message_id == "999"


def test_send_job_offer_includes_apply_and_skip_buttons(monkeypatch):
    calls = []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr(requests, "post", _capture_post(calls))

    provider = TelegramProvider()
    provider.send_job_offer({"id": "app-1"}, {"title": "Java Developer", "company_name": "ABC Corp"})

    assert len(calls) == 1
    payload = calls[0][1]
    assert "Java Developer" in payload["text"]
    buttons = payload["reply_markup"]["inline_keyboard"][0]
    assert [b["callback_data"] for b in buttons] == ["APPLY", "SKIP"]


# job-offer copy no longer claims a check happened before any wizard
# inspection occurred, and no longer hardcodes C2C/Easy Apply -- it's
# derived from the job's own stored fields.
def test_send_job_offer_does_not_claim_application_check_complete(monkeypatch):
    calls = []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr(requests, "post", _capture_post(calls))

    provider = TelegramProvider()
    provider.send_job_offer({"id": "app-1"}, {"title": "Java Developer", "company_name": "ABC Corp"})

    assert "Application check complete" not in calls[0][1]["text"]


def test_send_job_offer_includes_metadata_line_when_job_confirms_it(monkeypatch):
    calls = []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr(requests, "post", _capture_post(calls))

    provider = TelegramProvider()
    provider.send_job_offer({"id": "app-1"}, {"title": "Java Developer", "company_name": "ABC Corp", "c2c_status": "CONFIRMED", "is_easy_apply": True})

    assert "C2C • Easy Apply" in calls[0][1]["text"]


def test_send_job_offer_omits_metadata_line_when_job_has_neither(monkeypatch):
    calls = []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr(requests, "post", _capture_post(calls))

    provider = TelegramProvider()
    provider.send_job_offer({"id": "app-1"}, {"title": "Java Developer", "company_name": "ABC Corp", "c2c_status": "UNKNOWN", "is_easy_apply": False})

    assert "C2C" not in calls[0][1]["text"]
    assert "Easy Apply" not in calls[0][1]["text"]


def test_send_apply_ack_and_skip_ack_and_answer_accepted_and_ready_to_submit(monkeypatch):
    calls = []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr(requests, "post", _capture_post(calls))

    provider = TelegramProvider()
    provider.send_apply_ack("app-1")
    provider.send_skip_ack("app-1")
    provider.send_answer_accepted("app-1", "q-1")
    provider.send_ready_to_submit("app-1")

    texts = [c[1]["text"] for c in calls]
    assert "Checking" in texts[0]
    assert "Skipped" in texts[1]
    assert "Got it" in texts[2]
    assert "Submitting" in texts[3]


def test_send_missing_question_with_choices_uses_buttons(monkeypatch):
    calls = []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr(requests, "post", _capture_post(calls))

    provider = TelegramProvider()
    provider.send_missing_question("app-1", {"question_text": "Are you 18 or older?", "options": {"choices": ["Yes", "No"]}})

    payload = calls[0][1]
    buttons = payload["reply_markup"]["inline_keyboard"][0]
    assert [b["text"] for b in buttons] == ["Yes", "No"]
    assert [b["callback_data"] for b in buttons] == ["ANSWER:Yes", "ANSWER:No"]


def test_send_missing_question_without_choices_has_no_buttons(monkeypatch):
    calls = []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr(requests, "post", _capture_post(calls))

    provider = TelegramProvider()
    provider.send_missing_question("app-1", {"question_text": "What is your current city of residence?", "options": {}})

    payload = calls[0][1]
    assert "reply_markup" not in payload


def test_clear_buttons_sends_empty_inline_keyboard(monkeypatch):
    calls = []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(requests, "post", _capture_post(calls))

    provider = TelegramProvider()
    provider.clear_buttons("12345", "999")

    assert len(calls) == 1
    url, payload = calls[0]
    assert "editMessageReplyMarkup" in url
    assert payload == {"chat_id": "12345", "message_id": "999", "reply_markup": {"inline_keyboard": []}}


def test_clear_buttons_never_raises_on_failure(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")

    def _boom(*a, **kw):
        raise ConnectionError("network down")

    monkeypatch.setattr(requests, "post", _boom)
    TelegramProvider().clear_buttons("12345", "999")  # must not raise


def test_send_typing_indicator_sends_the_typing_action(monkeypatch):
    calls = []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(requests, "post", _capture_post(calls))

    TelegramProvider().send_typing_indicator("12345")

    assert len(calls) == 1
    url, payload = calls[0]
    assert "sendChatAction" in url
    assert payload == {"chat_id": "12345", "action": "typing"}


def test_send_typing_indicator_never_raises_on_failure(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")

    def _boom(*a, **kw):
        raise ConnectionError("network down")

    monkeypatch.setattr(requests, "post", _boom)
    TelegramProvider().send_typing_indicator("12345")  # must not raise
