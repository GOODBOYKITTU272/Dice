"""Phase 7.4: IMessageProvider -- fully offline. subprocess.run (the real
osascript send) and read_new_messages (the real chat.db read) are both
mocked; parse_inbound() itself never touches either. Never sends a real
iMessage, never needs Messages.app automation permission, to run."""
from __future__ import annotations

import subprocess

from attention.models import AttentionAction
from attention.providers import imessage as imessage_module
from attention.providers.imessage import IMessageProvider


# 22. iMessage APPLY reply normalizes correctly
def test_parse_inbound_apply_text_reply():
    provider = IMessageProvider()
    event = provider.parse_inbound({"rowid": 1, "text": "APPLY"})
    assert event.action == AttentionAction.APPLY
    assert event.external_message_id == "1"


def test_parse_inbound_apply_is_case_insensitive():
    provider = IMessageProvider()
    event = provider.parse_inbound({"rowid": 2, "text": "apply"})
    assert event.action == AttentionAction.APPLY


# 23. iMessage SKIP reply normalizes correctly
def test_parse_inbound_skip_text_reply():
    provider = IMessageProvider()
    event = provider.parse_inbound({"rowid": 3, "text": "  skip  "})
    assert event.action == AttentionAction.SKIP


# 24. iMessage CONFIRM/EDIT normalize correctly
def test_parse_inbound_confirm_and_edit_text_replies():
    provider = IMessageProvider()
    assert provider.parse_inbound({"rowid": 4, "text": "CONFIRM"}).action == AttentionAction.CONFIRM
    assert provider.parse_inbound({"rowid": 5, "text": "EDIT"}).action == AttentionAction.EDIT


def test_parse_inbound_free_text_falls_back_to_answer():
    provider = IMessageProvider()
    event = provider.parse_inbound({"rowid": 6, "text": "West Haven, CT"})
    assert event.action == AttentionAction.ANSWER
    assert event.raw_text == "West Haven, CT"


def test_parse_inbound_external_message_id_is_the_chat_db_rowid():
    provider = IMessageProvider()
    event = provider.parse_inbound({"rowid": 42, "text": "APPLY"})
    assert event.external_message_id == "42"


def test_send_job_offer_invokes_osascript_with_configured_contact(monkeypatch):
    calls = []
    monkeypatch.setenv("IMESSAGE_CONTACT", "+15551234567")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: calls.append((a, kw)))

    provider = IMessageProvider()
    provider.send_job_offer({"id": "app-1"}, {"title": "Java Developer", "company_name": "ABC Corp"})

    assert len(calls) == 1
    args = calls[0][0][0]
    assert args[0] == "osascript"
    script = args[2]
    assert "+15551234567" in script
    assert "Java Developer" in script
    assert "Reply:\\nAPPLY\\nor\\nSKIP" in script or "Reply:\nAPPLY\nor\nSKIP" in script


def test_send_missing_question_lists_choices_as_reply_hint(monkeypatch):
    calls = []
    monkeypatch.setenv("IMESSAGE_CONTACT", "+15551234567")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: calls.append((a, kw)))

    provider = IMessageProvider()
    provider.send_missing_question("app-1", {"question_text": "Are you 18 or older?", "options": {"choices": ["Yes", "No"]}})

    script = calls[0][0][0][2]
    assert "Yes" in script and "No" in script


def test_send_without_configured_contact_raises(monkeypatch):
    monkeypatch.delenv("IMESSAGE_CONTACT", raising=False)
    provider = IMessageProvider()
    try:
        provider.send_job_offer({"id": "app-1"}, {"title": "Java Developer", "company_name": "ABC Corp"})
        assert False, "expected IMessageNotConfiguredError"
    except imessage_module.IMessageNotConfiguredError:
        pass


def test_read_new_messages_returns_empty_when_chat_db_missing(monkeypatch):
    monkeypatch.setattr(imessage_module, "_CHAT_DB_PATH", imessage_module.Path("/nonexistent/chat.db"))
    assert imessage_module.read_new_messages("+15551234567") == []
