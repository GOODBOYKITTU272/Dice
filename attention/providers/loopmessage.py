"""Phase 7.11: LoopMessage adapter -- real iMessage sends/receives over
HTTP, replacing the local osascript/chat.db approach for production use.
Same "IMESSAGE" channel as attention/providers/imessage.py (this is
still iMessage from the candidate's point of view, just a different
transport) -- candidate_attention_channels rows, dedupe, and routing all
keep working unchanged.

No native buttons (LoopMessage's send API has no interactive-reply
field, confirmed 2026-08-24 against their own docs) -- every prompt asks
for a plain-text reply, identical wording to attention/providers/
imessage.py's own text-reply protocol.

LOOPMESSAGE_AUTH_KEY is read from the environment only -- never
hardcoded, never committed. This module makes a real HTTP call only
when send_*()/parse_inbound() are actually invoked; importing it does
not require the key to be configured.
"""
from __future__ import annotations

import os

import requests

from attention.formatting import job_metadata_line
from attention.models import AttentionAction, NormalizedEvent

_SEND_URL = "https://a.loopmessage.com/api/v1/message/send/"
_AUTH_KEY_ENV_VAR = "LOOPMESSAGE_AUTH_KEY"
_SENDER_ENV_VAR = "LOOPMESSAGE_SENDER_NAME"
_CONTACT_ENV_VAR = "LOOPMESSAGE_CONTACT"
_DEFAULT_TIMEOUT_SECONDS = 10.0


class LoopMessageNotConfiguredError(RuntimeError):
    pass


def _auth_key() -> str:
    key = os.environ.get(_AUTH_KEY_ENV_VAR)
    if not key:
        raise LoopMessageNotConfiguredError(f"{_AUTH_KEY_ENV_VAR} is not configured")
    return key


class LoopMessageProvider:
    channel = "IMESSAGE"

    def __init__(self, contact: str | None = None):
        """contact, when given, overrides LOOPMESSAGE_CONTACT for every
        send from this instance -- same reasoning as TelegramProvider's
        chat_id override: lets attention.service send to a candidate's
        real bound contact (candidate_attention_channels) rather than
        the env var being the permanent identity source."""
        self._contact_override = contact

    def _send(self, text: str) -> str:
        contact = self._contact_override or os.environ.get(_CONTACT_ENV_VAR)
        if not contact:
            raise LoopMessageNotConfiguredError(f"no contact bound and {_CONTACT_ENV_VAR} is not configured")
        payload: dict = {"contact": contact, "text": text}
        sender = os.environ.get(_SENDER_ENV_VAR)
        if sender:
            payload["sender"] = sender
        resp = requests.post(
            _SEND_URL,
            headers={"Authorization": _auth_key(), "Content-Type": "application/json"},
            json=payload,
            timeout=_DEFAULT_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        return resp.json()["message_id"]

    def send_job_offer(self, application: dict, job: dict) -> str:
        meta = job_metadata_line(job)
        meta_suffix = f"\n{meta}" if meta else ""
        text = (
            f"Found a match:\n\n{job['title']} — {job.get('company_name') or 'Unknown Company'}{meta_suffix}\n\n"
            'Reply "yes apply" to apply, or "no skip" to skip'
        )
        return self._send(text)

    def send_apply_ack(self, application_id: str) -> str:
        return self._send("Checking the application...")

    def send_skip_ack(self, application_id: str) -> str:
        return self._send("Skipped 👍\n\nI won't apply to this job.")

    def send_answer_accepted(self, application_id: str, question_id: str) -> str:
        return self._send("Got it ✅")

    def send_ready_to_submit(self, application_id: str) -> str:
        return self._send("✅ I now have everything needed.\n\nSubmitting your application now...")

    def send_missing_question(self, application_id: str, question: dict) -> str:
        prompt = question.get("question_text") or "I need one answer before I can continue."
        options = (question.get("options") or {}).get("choices")
        reply_hint = "\n".join(options) if options else "your answer"
        text = f"{prompt}\n\nReply:\n{reply_hint}"
        return self._send(text)

    def send_answer_confirmation(self, application_id: str, question_id: str, raw_answer: str) -> str:
        text = f'You answered:\n{raw_answer}\n\nReply "yes confirm" to confirm, or "no edit" to change it'
        return self._send(text)

    def send_submission_success(self, application: dict, job: dict) -> str:
        meta = job_metadata_line(job)
        meta_suffix = f"\n{meta}" if meta else ""
        text = f"Applied successfully ✅\n\n{job['title']}{meta_suffix}\n\nYour application was submitted."
        return self._send(text)

    def send_submission_failure(self, application: dict, job: dict, reason: str) -> str:
        text = f"Couldn't complete this application.\n\n{job['title']}\n{job.get('company_name') or ''}\n\n{reason}".rstrip()
        return self._send(text)

    def send_reconnect_required(self, application_id: str) -> str:
        text = "Dice needs to be reconnected.\n\nYour application is saved. Reconnect Dice and I'll continue automatically."
        return self._send(text)

    def send_reconnect_success(self, application: dict, job: dict) -> str:
        return self._send(f"Dice connected. I'm continuing your application for {job['title']} now.")

    def parse_inbound(self, raw_event: dict) -> NormalizedEvent:
        """raw_event is one LoopMessage webhook payload --
        {"event": "message_inbound", "contact", "text", "message_id", ...}.
        external_message_id is LoopMessage's own message_id (a real,
        unique-per-message UUID -- exactly what inbound dedupe needs),
        never re-derived or guessed.

        Real, live-found 2026-08-24: LoopMessage's own inbound pipeline
        silently drops short bare-word replies (exactly "APPLY", "SKIP",
        etc.) by misclassifying them as OTP codes -- confirmed by their
        support, who recommended "regular sentences" instead. Matching
        is containment-based (keyword anywhere in the text, case-
        insensitive) rather than exact-equality specifically so natural
        phrases like "yes apply" or "no thanks skip" resolve correctly
        -- send_job_offer/send_answer_confirmation's own prompts ask for
        exactly this style of reply now. None of the four keywords is a
        substring of another, so order never matters here."""
        external_message_id = str(raw_event["message_id"])
        text = (raw_event.get("text") or "").strip()
        lower = text.lower()
        for keyword in ("apply", "skip", "confirm", "edit"):
            if keyword in lower:
                return NormalizedEvent(channel=self.channel, external_message_id=external_message_id, action=AttentionAction(keyword.upper()))
        return NormalizedEvent(channel=self.channel, external_message_id=external_message_id, action=AttentionAction.ANSWER, raw_text=text)
